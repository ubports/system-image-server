# -*- coding: utf-8 -*-

# Copyright (C) 2013 Canonical Ltd.
# Author: Stéphane Graber <stgraber@ubuntu.com>

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 3 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from io import BytesIO
import gpgme
import os
import subprocess
import tarfile
import time


def generate_version_tarball(path, version, in_path="system/etc/ubuntu-build"):
    """
        Generates a tarball which contains a single file (in_path).
        That file contains the version string (version).
        The resulting tarball is written at the provided location (path).
    """

    tarball = tarfile.open(path, 'w:')

    version_file = tarfile.TarInfo()
    version_file.size = len(version)
    version_file.mtime = int(time.strftime("%s", time.gmtime()))
    version_file.name = in_path

    tarball.addfile(version_file, BytesIO(version.encode('utf-8')))

    tarball.close()


def xz_compress(path, destination=None, level=9):
    """
        Compress a file (path) using xz.
        By default, creates a .xz version of the file in the same directory.
        An alternate destination path may be provided.
        The compress level is 9 by default but can be overriden.
    """

    # NOTE: Once we can drop support for < 3.3, the new lzma module can be used

    if not destination:
        destination = "%s.xz" % path

    if os.path.exists(destination):
        raise Exception("destination already exists.")

    with open(destination, "wb+") as fd:
        retval = subprocess.call(['xz', '-z', '-%s' % level, '-c', path],
                                 stdout=fd)
    return retval


def xz_uncompress(path, destination=None):
    """
        Uncompress a file (path) using xz.
        By default, uses the source path without the .xz prefix as the target.
        An alternate destination path may be provided.
    """

    # NOTE: Once we can drop support for < 3.3, the new lzma module can be used

    if not destination and path[-3:] != ".xz":
        raise Exception("unspecified destination and path doesn't end"
                        " with .xz")

    if not destination:
        destination = path[:-3]

    if os.path.exists(destination):
        raise Exception("destination already exists.")

    with open(destination, "wb+") as fd:
        retval = subprocess.call(['xz', '-d', '-c', path],
                                 stdout=fd)

    return retval

def sign_file(key, path, destination=None, detach=True, armor=True):
    """
        Sign a file and publish the signature.
        The key parameter must be a valid key unders gpg/keys/.
        The path must be that of a valid file.
        The destination defaults to <path>.gpg (non-armored) or
        <path>.asc (armored).
        The detach and armor parameters respectively control the use of
        detached signatures and base64 armoring.
    """

    if not os.path.isdir("gpg/keys/%s" % key):
        raise IndexError("Invalid GPG key name '%s'." % key)

    if not os.path.isfile(path):
        raise Exception("Invalid path '%s'." % path)

    if not destination:
        if armor:
            destination = "%s.asc" % path
        elif detach:
            destination = "%s.sig" % path
        else:
            destination = "%s.gpg" % path

    if os.path.exists(destination):
        raise Exception("destination already exists.")

    os.environ['GNUPGHOME'] = "gpg/keys/%s" % key

    # Create a GPG context, assuming no passphrase
    ctx = gpgme.Context()
    ctx.armor = armor
    [key] = ctx.keylist()
    ctx.signers = [key]

    with open(path, "rb") as fd_in, open(destination, "wb+") as fd_out:
        if detach:
            retval = ctx.sign(fd_in, fd_out, gpgme.SIG_MODE_DETACH)
        else:
            retval = ctx.sign(fd_in, fd_out, gpgme.SIG_MODE_NORMAL)

    return retval
