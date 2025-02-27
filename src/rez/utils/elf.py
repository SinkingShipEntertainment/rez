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


"""
Functions that wrap readelf/patchelf utils on linux.
"""
import os
import pipes
import subprocess

from rez.utils.filesystem import make_path_writable


def get_rpaths(elfpath):
    """Get rpaths/runpaths from header.
    """

    # stdout lines look like:
    # 0x000000000000000f (RPATH) Library rpath: [/xxx:/yyy]
    #
    out = _run("readelf", "-d", elfpath)

    # parse out rpath/runpath
    for line in out.split('\n'):
        parts = line.strip().split()
        if "(RPATH)" in parts or "(RUNPATH)" in parts:
            txt = parts[-1]
            txt = txt[1:-1]  # strip [ and ]
            rpaths = txt.split(':')

            return rpaths or []

    return []


def patch_rpaths(elfpath, rpaths):
    """Replace an elf's rpath header with those provided.
    """

    # this is a hack to get around https://github.com/nerdvegas/rez/issues/1074
    # I actually hit a case where patchelf was installed as a rez suite tool,
    # causing '$ORIGIN' to be expanded early (to empty string).
    # TODO remove this hack when bug is fixed
    #
    env = os.environ.copy()
    env["ORIGIN"] = "$ORIGIN"

    with make_path_writable(elfpath):
        if rpaths:
            _run("patchelf", "--set-rpath", ':'.join(rpaths), elfpath, env=env)
        else:
            _run("patchelf", "--remove-rpath")


def _run(*nargs, **popen_kwargs):
    proc = subprocess.Popen(
        nargs,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **popen_kwargs
    )

    out, err = proc.communicate()

    if proc.returncode:
        cmd_ = ' '.join(pipes.quote(x) for x in nargs)

        raise RuntimeError(
            "Command %s - failed with exitcode %d: %s"
            % (cmd_, proc.returncode, err.strip().replace('\n', "\\n"))
        )

    return out
