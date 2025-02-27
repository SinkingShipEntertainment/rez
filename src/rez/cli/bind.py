# Copyright Contributors to the Rez project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


'''
Create a Rez package for existing software.
'''
from __future__ import print_function

import argparse


def setup_parser(parser, completions=False):
    parser.add_argument(
        "--quickstart", action="store_true",
        help="bind a set of standard packages to get started")
    parser.add_argument(
        "-r", "--release", action="store_true",
        help="install to release path; overrides -i")
    parser.add_argument(
        "-i", "--install-path", dest="install_path", type=str,
        default=None, metavar="PATH",
        help="install path, defaults to local package path")
    parser.add_argument(
        "--no-deps", dest="no_deps", action="store_true",
        help="Do not bind dependencies")
    parser.add_argument(
        "-l", "--list", action="store_true",
        help="list all available bind modules")
    parser.add_argument(
        "-s", "--search", action="store_true",
        help="search for the bind module but do not perform the bind")
    parser.add_argument(
        "PKG", type=str, nargs='?',
        help='package to bind')
    parser.add_argument(
        "BIND_ARGS", metavar="ARG", nargs=argparse.REMAINDER,
        help="extra arguments to the target bind module. Use '-h' to show help "
        "for the module")


def command(opts, parser, extra_arg_groups=None):
    from rez.config import config
    from rez.package_bind import bind_package, find_bind_module, \
        get_bind_modules, _print_package_list
    from rez.utils.formatting import PackageRequest, columnise

    if opts.release:
        install_path = config.release_packages_path
    elif opts.install_path:
        install_path = opts.install_path
    else:
        install_path = config.local_packages_path

    if opts.list:
        d = get_bind_modules()
        rows = [["PACKAGE", "BIND MODULE"],
                ["-------", "-----------"]]
        rows += sorted(d.items())
        print('\n'.join(columnise(rows)))
        return

    if opts.quickstart:
        # note: in dependency order, do not change
        names = ["platform",
                 "arch",
                 "os",
                 "python",
                 "rez",
                 "rezgui",
                 "setuptools",
                 "pip"]

        variants = []

        for name in names:
            print("Binding %s into %s..." % (name, install_path))
            variants_ = bind_package(name,
                                     path=install_path,
                                     no_deps=True,
                                     quiet=True)
            variants.extend(variants_)

        if variants:
            print("\nSuccessfully converted the following software found on "
                  "the current system into Rez packages:")
            print()
            _print_package_list(variants)

        print("\nTo bind other software, see what's available using the "
              "command 'rez-bind --list', then run 'rez-bind <name>'.\n")

        return

    if not opts.PKG:
        parser.error("PKG required.")

    req = PackageRequest(opts.PKG)
    name = req.name
    version_range = None if req.range.is_any() else req.range

    if opts.search:
        find_bind_module(name, verbose=True)
    else:
        bind_package(name,
                     path=install_path,
                     version_range=version_range,
                     no_deps=opts.no_deps,
                     bind_args=opts.BIND_ARGS)
