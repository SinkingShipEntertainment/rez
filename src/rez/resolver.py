from rez.solver import Solver, SolverStatus, PackageVariantCache
from rez.package_repository import package_repository_manager
from rez.memcache import memcache_client, DataType
from rez.packages_ import get_variant, get_last_release_time
from rez.config import config
from rez.vendor.enum import Enum
import os


class ResolverStatus(Enum):
    """ Enum to represent the current state of a resolver instance.  The enum
    also includes a human readable description of what the state represents.
    """

    pending = ("The resolve has not yet started.", )
    solved = ("The resolve has completed successfully.", )
    failed = ("The resolve is not possible.", )
    aborted = ("The resolve was stopped by the user (via callback).", )

    def __init__(self, description):
        self.description = description


class Resolver(object):
    """The package resolver.

    The Resolver uses a combination of Solver(s) and cache(s) to resolve a
    package request as quickly as possible.
    """
    def __init__(self, package_requests, package_paths, timestamp=0,
                 callback=None, building=False, verbosity=False, buf=None,
                 package_load_callback=None, max_depth=0, start_depth=0,
                 caching=True):
        """Create a Resolver.

        Args:
            package_requests: List of Requirement objects representing the
                request.
            package_paths: List of paths to search for pkgs.
            callback: See `Solver`.
            package_load_callback: If not None, this callable will be called
                prior to each package being loaded. It is passed a single
                `Package` object.
            building: True if we're resolving for a build.
            max_depth (int): If non-zero, this value limits the number of packages
                that can be loaded for any given package name. This effectively
                trims the search space - only the highest N package versions are
                searched.
            start_depth (int): If non-zero, an initial solve is performed with
                `max_depth` set to this value. If this fails, the depth is doubled,
                and another solve is performed. If `start_depth` is specified but
                `max_depth` is not, the solve will iterate until all relevant
                packages have been loaded. Using this argument  allows us to
                perform something like a breadth-first search - we put off
                loading older packages with the assumption that they aren't being
                used anymore.
            caching: If True, cache(s) may be used to speed the resolve. If
                False, caches will not be used.
        """
        self.package_requests = package_requests
        self.package_paths = package_paths
        self.timestamp = timestamp
        self.callback = callback
        self.package_load_callback = package_load_callback
        self.building = building
        self.verbosity = verbosity
        self.caching = caching
        self.buf = buf

        self.max_depth = max_depth
        self.start_depth = start_depth
        if self.max_depth and self.start_depth:
            assert self.max_depth >= self.start_depth

        self.status_ = ResolverStatus.pending
        self.resolved_packages_ = None
        self.failure_description = None
        self.graph_ = None
        self.from_cache = False

        self.solve_time = 0.0  # time spent solving
        self.load_time = 0.0   # time spent loading package resources

        self._print = config.debug_printer("resolve_memcache")

    def solve(self):
        """Perform the solve.
        """
        solver_dict = self._get_cached_solve()
        if solver_dict:
            self.from_cache = True
            self._set_result(solver_dict)
        else:
            self.from_cache = False
            solver = self._solve()
            solver_dict = self._solver_to_dict(solver)
            self._set_result(solver_dict)
            self._set_cached_solve(solver_dict)

    @property
    def status(self):
        """Return the current status of the resolve.

        Returns:
          ResolverStatus.
        """
        return self.status_

    @property
    def resolved_packages(self):
        """Get the list of resolved packages.

        Returns:
            List of `PackageVariant` objects, or None if the resolve has not
            completed.
        """
        return self.resolved_packages_

    @property
    def graph(self):
        """Return the resolve graph.

        The resolve graph shows unsuccessful as well as successful resolves.

        Returns:
            A pygraph.digraph object, or None if the solve has not completed.
        """
        return self.graph_

    def _get_cached_solve(self):
        """Find a memcached resolve.

        If there is NOT a resolve timestamp:
            - fetch a non-timestamped memcache entry;
            - if no entry, then fail;
            - if packages have changed, then:
              - delete the entry;
              -  fail;
            - if no packages in the entry have been released since, then
              - use the entry and return;
            - else:
              - delete the entry;
              - fail.

        If there IS a resolve timestamp (let us call this T):
            - fetch a non-timestamped memcache entry;
            - if entry then:
              - if no packages have changed, then:
                - if no packages in the entry have been released since:
                  - if no packages in the entry were released after T, then
                    - use the entry and return;
                - else:
                  - delete the entry;
              - else:
                - delete the entry;
            - fetch a timestamped (T) memcache entry;
            - if no entry, then fail;
            - if packages have changed, then:
              - delete the entry;
              - fail;
            - else:
              - use the entry.

        This behaviour exists specifically so that resolves that do use a
        timestamp but set that to the current time, can be reused by other
        resolves if nothing has changed. Older resolves however, can only be
        reused if the timestamp matches exactly (but this might happen a lot -
        consider a workflow where a work area is tied down to a particular
        timestamp in order to 'lock' it from any further software releases).
        """
        if not (config.resolve_caching
                and self.caching
                and memcache_client.enabled):
            return None

        def _hit(data):
            solver_dict, _, _ = data
            return solver_dict

        def _miss():
            self._print("No cache key retrieved")
            return None

        def _delete_cache_entry(key):
            memcache_client.delete(DataType.resolve, key)
            self._print("Discarded entry: %r", key)

        def _retrieve(timestamped):
            key = self._memcache_key(timestamped=timestamped)
            self._print("Retrieving memcache key: %r", key)
            data = memcache_client.get(DataType.resolve, key)
            return key, data

        def _packages_changed(key, data):
            solver_dict, _, variant_states_dict = data
            for variant_handle in solver_dict.get("variant_handles", []):
                variant = get_variant(variant_handle)
                old_state = variant_states_dict.get(variant.name)
                repo = variant.resource._repository
                new_state = repo.get_variant_state_handle(variant.resource)

                if old_state != new_state:
                    self._print("%r has been modified", variant.qualified_name)
                    return True
            return False

        def _releases_since_solve(key, data):
            _, release_times_dict, _ = data
            for package_name, release_time in release_times_dict.iteritems():
                time_ = get_last_release_time(package_name, self.package_paths)
                if time_ != release_time:
                    self._print(
                        "A newer version of %r (%d) has been released since the "
                        "resolve was cached (latest release in cache was %d) "
                        "(entry: %r)", package_name, time_, release_time, key)
                    return True
            return False

        def _timestamp_is_earlier(key, data):
            _, release_times_dict, _ = data
            for package_name, release_time in release_times_dict.iteritems():
                if self.timestamp < release_time:
                    self._print("Resolve timestamp (%d) is earlier than %r in "
                                "solve (%d) (entry: %r)", self.timestamp,
                                package_name, release_time, key)
                    return True
            return False

        key, data = _retrieve(False)

        if self.timestamp:
            if data:
                if _packages_changed(key, data):
                    _delete_cache_entry(key)
                elif _releases_since_solve(key, data):
                    _delete_cache_entry(key)
                elif not _timestamp_is_earlier(key, data):
                    return _hit()

            key, data = _retrieve(True)
            if not data:
                return _miss()
            if _packages_changed(key, data):
                _delete_cache_entry(key)
                return _miss()
            else:
                return _hit(data)
        else:
            if not data:
                return _miss()
            if _packages_changed(key, data):
                _delete_cache_entry(key)
                return _miss()
            if _releases_since_solve(key, data):
                _delete_cache_entry(key)
                return _miss()
            else:
                return _hit(data)



        """
        data = _retrieve(False)
        if not data and not self.timestamp:
            return _miss()

        if self.timestamp:
            pass
        elif _releases_since_solve(data):
            return _miss()
        else:
            pass



        # find a memcached resolve. This only works for a resolve whos
        # timestamp is >= the most recent release of any package in the
        # resolve.
        if not (self.caching and memcache_client.enabled):
            return None

        key = self._memcache_key
        self._print("Retrieving memcache key: %r", key)

        data = memcache_client.get(DataType.resolve, key)
        if data is None:
            self._print("No cache key retrieved")
            return None

        solver_dict, release_times_dict, variant_states_dict = data

        def _delete_cache_entry():
            memcache_client.delete(DataType.resolve, key)

        # discard if timestamp is < any most recent package release
        if self.timestamp:
            for package_name, release_time in release_times_dict.iteritems():
                if self.timestamp < release_time:
                    self._print("Discarded entry: resolve timestamp (%d) is "
                                "earlier than latest %r release (%d)",
                                self.timestamp, package_name, release_time)
                    return None

        # check for newer package releases, this invalidates the cache entry
        for package_name, release_time in release_times_dict.iteritems():
            time_ = get_last_release_time(package_name, self.package_paths)

            if time_ != release_time:
                _delete_cache_entry()
                self._print("Discarded entry: a newer version of %r (%d) has "
                            "been released since the resolve was cached "
                            "(latest release in cache was %d)",
                            package_name, time_, release_time)
                return None

        # check for changed variants (for example, a modified package.py).
        # Packages in theory should not change after being installed/released,
        # but in practise people do change them (especially if it's local)
        for variant_handle in solver_dict.get("variant_handles", []):
            variant = get_variant(variant_handle)
            old_state = variant_states_dict.get(variant.name)
            repo = variant.resource._repository
            new_state = repo.get_variant_state_handle(variant.resource)

            if old_state != new_state:
                _delete_cache_entry()
                self._print("Discarded entry: %r has been modified"
                            % variant.qualified_name)
                return None

        return solver_dict
        """

    def _set_cached_solve(self, solver_dict):
        """Store a solve to memcached.

        If there is NOT a resolve timestamp:
            - store the solve to a non-timestamped entry.

        If there IS a resolve timestamp (let us call this T):
            - if NO newer package in the solve has been released since T,
              - then store the solve to a non-timestamped entry;
            - else:
              - store the solve to a timestamped entry.
        """
        if not (config.resolve_caching
                and self.caching
                and memcache_client.enabled):
            return

        if self.status_ != ResolverStatus.solved:
            return  # don't cache failed solves

        # most recent release times get stored with solve result in the cache
        releases_since_solve = False
        release_times_dict = {}
        variant_states_dict = {}

        for variant in self.resolved_packages_:
            time_ = get_last_release_time(variant.name, self.package_paths)

            # don't cache if a release time isn't known
            if time_ == 0:
                self._print("Did not send memcache key: a repository could "
                            "not provide a most recent release time for %r",
                            variant.name)
                return

            if self.timestamp and self.timestamp < time_:
                releases_since_solve = True

            release_times_dict[variant.name] = time_
            repo = variant.resource._repository
            variant_states_dict[variant.name] = \
                repo.get_variant_state_handle(variant.resource)

        timestamped = (self.timestamp and releases_since_solve)
        key = self._memcache_key(timestamped=timestamped)
        data = (solver_dict, release_times_dict, variant_states_dict)
        memcache_client.set(DataType.resolve, key, data)
        self._print("Sent memcache key: %r", key)

    def _memcache_key(self, timestamped=False):
        """Makes a key suitable as a memcache entry."""
        request = tuple(map(str, self.package_requests))
        repo_ids = []
        for path in self.package_paths:
            repo = package_repository_manager.get_repository(path)
            repo_ids.append(repo.uid)

        t = [request,
             tuple(repo_ids),
             self.building,
             config.prune_failed_graph,
             self.start_depth,
             self.max_depth]
        if timestamped and self.timestamp:
            t.append(self.timestamp)

        return tuple(t)

    def _solve(self):
        package_cache = PackageVariantCache(
            self.package_paths,
            timestamp=self.timestamp,
            package_load_callback=self.package_load_callback,
            building=self.building)

        kwargs = dict(package_requests=self.package_requests,
                      package_cache=package_cache,
                      package_paths=self.package_paths,
                      timestamp=self.timestamp,
                      callback=self.callback,
                      package_load_callback=self.package_load_callback,
                      building=self.building,
                      verbosity=self.verbosity,
                      prune_unfailed=config.prune_failed_graph,
                      buf=self.buf)

        if self.start_depth:
            # perform an iterative solve, doubling search depth until a solution
            # is found or all packages are exhausted
            depth = self.start_depth

            while True:
                solver = Solver(max_depth=depth, **kwargs)
                solver.pr.header("SOLVING TO DEPTH %d..." % depth)
                solver.solve()

                if not solver.is_partial \
                        or solver.status == SolverStatus.solved \
                        or self.max_depth and depth >= self.max_depth:
                    break
                else:
                    depth *= 2
                    if self.max_depth:
                        depth = min(depth, self.max_depth)

        elif self.max_depth:
            # perform a solve that loads only the first N packages of any
            # given package request in the solve
            solver = Solver(max_depth=self.max_depth, **kwargs)
            solver.solve()
        else:
            # perform a solve that loads all relevant packages
            solver = Solver(**kwargs)
            solver.solve()

        return solver

    def _set_result(self, solver_dict):
        self.status_ = solver_dict.get("status")
        self.graph_ = solver_dict.get("graph")
        self.solve_time = solver_dict.get("solve_time")
        self.load_time = solver_dict.get("load_time")
        self.failure_description = solver_dict.get("failure_description")

        self.resolved_packages_ = None
        if self.status_ == ResolverStatus.solved:
            # convert solver.Variants to packages.Variants
            self.resolved_packages_ = []
            for variant_handle in solver_dict.get("variant_handles", []):
                variant = get_variant(variant_handle)
                self.resolved_packages_.append(variant)

    @classmethod
    def _solver_to_dict(cls, solver):
        graph_ = solver.get_graph()
        solve_time = solver.solve_time
        load_time = solver.load_time
        failure_description = None
        variant_handles = None

        st = solver.status
        if st == SolverStatus.unsolved:
            status_ = ResolverStatus.aborted
            failure_description = solver.abort_reason
        elif st == SolverStatus.failed:
            status_ = ResolverStatus.failed
            failure_description = solver.failure_description()
        elif st == SolverStatus.solved:
            status_ = ResolverStatus.solved

            variant_handles = []
            for solver_variant in solver.resolved_packages:
                variant_handle_dict = solver_variant.userdata
                variant_handles.append(variant_handle_dict)

        return dict(
            status=status_,
            graph=graph_,
            solve_time=solve_time,
            load_time=load_time,
            failure_description=failure_description,
            variant_handles=variant_handles)
