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

from hashlib import sha256
from systemimage import diff, gpg, tree, tools
import json
import os
import socket
import shutil
import subprocess
import tarfile
import tempfile
import time

try:
    from urllib.request import urlopen, urlretrieve
except ImportError:  # pragma: no cover
    from urllib import urlopen, urlretrieve

# Global
CACHE = {}


def root_ownership(tarinfo):
    tarinfo.mode = 0o644
    tarinfo.mtime = int(time.strftime("%s", time.localtime()))
    tarinfo.uname = "root"
    tarinfo.gname = "root"
    return tarinfo


def unpack_arguments(arguments):
    """
        Takes a string representing comma separate key=value options and
        returns a dict.
    """
    arg_dict = {}

    for option in arguments.split(","):
        fields = option.split("=")
        if len(fields) != 2:
            continue

        arg_dict[fields[0]] = fields[1]

    return arg_dict


def generate_delta(conf, source_path, target_path):
    """
        Take two .tar.xz file and generate a third file, stored in the pool.
        The path to the pool file is then returned and <path>.asc is also
        generated using the default signing key.
    """
    source_filename = source_path.split("/")[-1].replace(".tar.xz", "")
    target_filename = target_path.split("/")[-1].replace(".tar.xz", "")

    # FIXME: This is a bit of an hack, it'd be better not to have to hardcode
    #        that kind of stuff...
    if (source_filename.startswith("version-")
            and target_filename.startswith("version-")):
        return target_path

    if (source_filename.startswith("keyring-")
            and target_filename.startswith("keyring-")):
        return target_path

    # Now for everything else
    path = os.path.realpath(os.path.join(conf.publish_path, "pool",
                                         "%s.delta-%s.tar.xz" %
                                         (target_filename, source_filename)))

    # Return pre-existing entries
    if os.path.exists(path):
        return path

    # Create the pool if it doesn't exist
    if not os.path.exists(os.path.join(conf.publish_path, "pool")):
        os.makedirs(os.path.join(conf.publish_path, "pool"))

    # Generate the diff
    tempdir = tempfile.mkdtemp()
    tools.xz_uncompress(source_path, os.path.join(tempdir, "source.tar"))
    tools.xz_uncompress(target_path, os.path.join(tempdir, "target.tar"))

    imagediff = diff.ImageDiff(os.path.join(tempdir, "source.tar"),
                               os.path.join(tempdir, "target.tar"))

    imagediff.generate_diff_tarball(os.path.join(tempdir, "output.tar"))
    tools.xz_compress(os.path.join(tempdir, "output.tar"), path)
    shutil.rmtree(tempdir)

    # Sign the result
    gpg.sign_file(conf, "image-signing", path)

    # Generate the metadata file
    metadata = {}
    metadata['generator'] = "delta"
    metadata['source'] = {}
    metadata['target'] = {}

    if os.path.exists(source_path.replace(".tar.xz", ".json")):
        with open(source_path.replace(".tar.xz", ".json"), "r") as fd:
            metadata['source'] = json.loads(fd.read())

    if os.path.exists(target_path.replace(".tar.xz", ".json")):
        with open(target_path.replace(".tar.xz", ".json"), "r") as fd:
            metadata['target'] = json.loads(fd.read())

    with open(path.replace(".tar.xz", ".json"), "w+") as fd:
        fd.write("%s\n" % json.dumps(metadata, sort_keys=True,
                                     indent=4, separators=(',', ': ')))
    gpg.sign_file(conf, "image-signing", path.replace(".tar.xz", ".json"))

    return path


def generate_file(conf, generator, arguments, environment):
    """
        Dispatcher for the various generators and importers.
        It calls the right generator and signs the generated file
        before returning the path.
    """

    if generator == "version":
        path = generate_file_version(conf, arguments, environment)
    elif generator == "cdimage-device":
        path = generate_file_cdimage_device(conf, arguments, environment)
    elif generator == "cdimage-ubuntu":
        path = generate_file_cdimage_ubuntu(conf, arguments, environment)
    elif generator == "http":
        path = generate_file_http(conf, arguments, environment)
    elif generator == "keyring":
        path = generate_file_keyring(conf, arguments, environment)
    elif generator == "system-image":
        path = generate_file_system_image(conf, arguments, environment)
    elif generator == "remote-system-image":
        path = generate_file_remote_system_image(conf, arguments, environment)
    else:
        raise Exception("Invalid generator: %s" % generator)

    return path


def generate_file_cdimage_device(conf, arguments, environment):
    """
        Scan a cdimage tree for new device files.
    """

    # We need at least a path and a series
    if len(arguments) < 2:
        return None

    # Read the arguments
    cdimage_path = arguments[0]
    series = arguments[1]

    options = {}
    if len(arguments) > 2:
        options = unpack_arguments(arguments[2])

    boot_arch = "armhf"
    recovery_arch = "armel"
    system_arch = "armel"
    if environment['device_name'] in ("generic_x86", "generic_i386"):
        boot_arch = "i386"
        recovery_arch = "i386"
        system_arch = "i386"
    elif environment['device_name'] in ("generic_amd64",):
        boot_arch = "amd64"
        recovery_arch = "amd64"
        system_arch = "amd64"

    # Check that the directory exists
    if not os.path.exists(cdimage_path):
        return None

    versions = sorted([version for version in os.listdir(cdimage_path)
                       if version not in ("pending", "current")],
                      reverse=True)

    for version in versions:
        # Skip directory without checksums
        if not os.path.exists(os.path.join(cdimage_path, version,
                                           "SHA256SUMS")):
            continue

        # Check for all the needed files
        boot_path = os.path.join(cdimage_path, version,
                                 "%s-preinstalled-boot-%s+%s.img" %
                                 (series, boot_arch,
                                  environment['device_name']))
        if not os.path.exists(boot_path):
            continue

        recovery_path = os.path.join(cdimage_path, version,
                                     "%s-preinstalled-recovery-%s+%s.img" %
                                     (series, recovery_arch,
                                      environment['device_name']))
        if not os.path.exists(recovery_path):
            continue

        system_path = os.path.join(cdimage_path, version,
                                   "%s-preinstalled-system-%s+%s.img" %
                                   (series, system_arch,
                                    environment['device_name']))
        if not os.path.exists(system_path):
            continue

        # Check if we should only import tested images
        if options.get("import", "any") == "good":
            if not os.path.exists(os.path.join(cdimage_path, version,
                                               ".marked_good")):
                continue

        # Set the version_detail string
        version_detail = "device=%s" % version

        # Extract the hashes
        boot_hash = None
        recovery_hash = None
        system_hash = None
        with open(os.path.join(cdimage_path, version,
                               "SHA256SUMS"), "r") as fd:
            for line in fd:
                line = line.strip()
                if line.endswith(boot_path.split("/")[-1]):
                    boot_hash = line.split()[0]
                elif line.endswith(recovery_path.split("/")[-1]):
                    recovery_hash = line.split()[0]
                elif line.endswith(system_path.split("/")[-1]):
                    system_hash = line.split()[0]

                if boot_hash and recovery_hash and system_hash:
                    break

        if not boot_hash or not recovery_hash or not system_hash:
            continue

        hash_string = "%s/%s/%s" % (boot_hash, recovery_hash, system_hash)
        global_hash = sha256(hash_string.encode('utf-8')).hexdigest()

        # Generate the path
        path = os.path.join(conf.publish_path, "pool",
                            "device-%s.tar.xz" % global_hash)

        # Return pre-existing entries
        if os.path.exists(path):
            # Get the real version number (in case it got copied)
            if os.path.exists(path.replace(".tar.xz", ".json")):
                with open(path.replace(".tar.xz", ".json"), "r") as fd:
                    metadata = json.loads(fd.read())

                if "version_detail" in metadata:
                    version_detail = metadata['version_detail']

            environment['version_detail'].append(version_detail)
            return path

        temp_dir = tempfile.mkdtemp()

        # Generate a new tarball
        target_tarball = tarfile.open(os.path.join(temp_dir, "target.tar"),
                                      "w:")

        # system image
        # # convert to raw image
        system_img = os.path.join(temp_dir, "system.img")
        with open(os.path.devnull, "w") as devnull:
            subprocess.call(["simg2img", system_path, system_img],
                            stdout=devnull)

        # # shrink to minimal size
        with open(os.path.devnull, "w") as devnull:
            subprocess.call(["resize2fs", "-M", system_img],
                            stdout=devnull, stderr=devnull)

        # # include in tarball
        target_tarball.add(system_img,
                           arcname="system/var/lib/lxc/android/system.img",
                           filter=root_ownership)

        # boot image
        target_tarball.add(boot_path, arcname="partitions/boot.img",
                           filter=root_ownership)

        # recovery image
        target_tarball.add(recovery_path,
                           arcname="partitions/recovery.img",
                           filter=root_ownership)

        target_tarball.close()

        # Create the pool if it doesn't exist
        if not os.path.exists(os.path.join(conf.publish_path, "pool")):
            os.makedirs(os.path.join(conf.publish_path, "pool"))

        # Compress the target tarball and sign it
        tools.xz_compress(os.path.join(temp_dir, "target.tar"), path)
        gpg.sign_file(conf, "image-signing", path)

        # Generate the metadata file
        metadata = {}
        metadata['generator'] = "cdimage-device"
        metadata['version'] = version
        metadata['version_detail'] = version_detail
        metadata['series'] = series
        metadata['device'] = environment['device_name']
        metadata['boot_path'] = boot_path
        metadata['boot_checksum'] = boot_hash
        metadata['recovery_path'] = recovery_path
        metadata['recovery_checksum'] = recovery_hash
        metadata['system_path'] = system_path
        metadata['system_checksum'] = system_hash

        with open(path.replace(".tar.xz", ".json"), "w+") as fd:
            fd.write("%s\n" % json.dumps(metadata, sort_keys=True,
                                         indent=4, separators=(',', ': ')))
        gpg.sign_file(conf, "image-signing", path.replace(".tar.xz", ".json"))

        # Cleanup
        shutil.rmtree(temp_dir)

        environment['version_detail'].append(version_detail)
        return path

    return None


def generate_file_cdimage_ubuntu(conf, arguments, environment):
    """
        Scan a cdimage tree for new ubuntu files.
    """

    # We need at least a path and a series
    if len(arguments) < 2:
        return None

    # Read the arguments
    cdimage_path = arguments[0]
    series = arguments[1]

    options = {}
    if len(arguments) > 2:
        options = unpack_arguments(arguments[2])

    arch = "armhf"
    if environment['device_name'] in ("generic_x86", "generic_i386"):
        arch = "i386"
    elif environment['device_name'] in ("generic_amd64",):
        arch = "amd64"

    # Check that the directory exists
    if not os.path.exists(cdimage_path):
        return None

    versions = sorted([version for version in os.listdir(cdimage_path)
                       if version not in ("pending", "current")],
                      reverse=True)

    for version in versions:
        # Skip directory without checksums
        if not os.path.exists(os.path.join(cdimage_path, version,
                                           "SHA256SUMS")):
            continue

        # Check for the rootfs
        rootfs_path = os.path.join(cdimage_path, version,
                                   "%s-preinstalled-%s-%s.tar.gz" %
                                   (series, options.get("product", "touch"),
                                    arch))
        if not os.path.exists(rootfs_path):
            continue

        # Check if we should only import tested images
        if options.get("import", "any") == "good":
            if not os.path.exists(os.path.join(cdimage_path, version,
                                               ".marked_good")):
                continue

        # Set the version_detail string
        version_detail = "ubuntu=%s" % version

        # Extract the hash
        rootfs_hash = None
        with open(os.path.join(cdimage_path, version,
                               "SHA256SUMS"), "r") as fd:
            for line in fd:
                line = line.strip()
                if line.endswith(rootfs_path.split("/")[-1]):
                    rootfs_hash = line.split()[0]
                    break

        if not rootfs_hash:
            continue

        # Generate the path
        path = os.path.join(conf.publish_path, "pool",
                            "ubuntu-%s.tar.xz" % rootfs_hash)

        # Return pre-existing entries
        if os.path.exists(path):
            # Get the real version number (in case it got copied)
            if os.path.exists(path.replace(".tar.xz", ".json")):
                with open(path.replace(".tar.xz", ".json"), "r") as fd:
                    metadata = json.loads(fd.read())

                if "version_detail" in metadata:
                    version_detail = metadata['version_detail']

            environment['version_detail'].append(version_detail)
            return path

        temp_dir = tempfile.mkdtemp()

        # Unpack the source tarball
        tools.gzip_uncompress(rootfs_path, os.path.join(temp_dir,
                                                        "source.tar"))

        # Generate a new shifted tarball
        source_tarball = tarfile.open(os.path.join(temp_dir, "source.tar"),
                                      "r:")
        target_tarball = tarfile.open(os.path.join(temp_dir, "target.tar"),
                                      "w:")

        added = []
        for entry in source_tarball:
            # FIXME: Will need to be done on the real rootfs
            # Skip some files
            if entry.name in ("SWAP.swap", "etc/mtab"):
                continue

            fileptr = None
            if entry.isfile():
                try:
                    fileptr = source_tarball.extractfile(entry.name)
                except KeyError:  # pragma: no cover
                    pass

            # Update hardlinks to point to the right target
            if entry.islnk():
                entry.linkname = "system/%s" % entry.linkname

            entry.name = "system/%s" % entry.name
            target_tarball.addfile(entry, fileobj=fileptr)
            added.append(entry.name)

        if options.get("product", "touch") == "touch":
            # FIXME: Will need to be done on the real rootfs
            # Add some symlinks and directories
            # # /android
            new_file = tarfile.TarInfo()
            new_file.type = tarfile.DIRTYPE
            new_file.name = "system/android"
            new_file.mode = 0o755
            new_file.mtime = int(time.strftime("%s", time.localtime()))
            new_file.uname = "root"
            new_file.gname = "root"
            target_tarball.addfile(new_file)

            # # Android partitions
            for android_path in ("cache", "data", "factory", "firmware",
                                 "persist", "system"):
                new_file = tarfile.TarInfo()
                new_file.type = tarfile.SYMTYPE
                new_file.name = "system/%s" % android_path
                new_file.linkname = "/android/%s" % android_path
                new_file.mode = 0o755
                new_file.mtime = int(time.strftime("%s", time.localtime()))
                new_file.uname = "root"
                new_file.gname = "root"
                target_tarball.addfile(new_file)

            # # /vendor
            new_file = tarfile.TarInfo()
            new_file.type = tarfile.SYMTYPE
            new_file.name = "system/vendor"
            new_file.linkname = "/android/system/vendor"
            new_file.mode = 0o755
            new_file.mtime = int(time.strftime("%s", time.localtime()))
            new_file.uname = "root"
            new_file.gname = "root"
            target_tarball.addfile(new_file)

        elif options.get("product", "touch") == "core":
            new_file = tarfile.TarInfo()
            new_file.type = tarfile.DIRTYPE
            new_file.name = "system/android/cache/recovery"
            new_file.mode = 0o755
            new_file.mtime = int(time.strftime("%s", time.localtime()))
            new_file.uname = "root"
            new_file.gname = "root"
            target_tarball.addfile(new_file)

        # # /userdata
        new_file = tarfile.TarInfo()
        new_file.type = tarfile.DIRTYPE
        new_file.name = "system/userdata"
        new_file.mode = 0o755
        new_file.mtime = int(time.strftime("%s", time.localtime()))
        new_file.uname = "root"
        new_file.gname = "root"
        target_tarball.addfile(new_file)

        # # /etc/mtab
        new_file = tarfile.TarInfo()
        new_file.type = tarfile.SYMTYPE
        new_file.name = "system/etc/mtab"
        new_file.linkname = "/proc/mounts"
        new_file.mode = 0o444
        new_file.mtime = int(time.strftime("%s", time.localtime()))
        new_file.uname = "root"
        new_file.gname = "root"
        target_tarball.addfile(new_file)

        # # /lib/modules
        new_file = tarfile.TarInfo()
        new_file.type = tarfile.DIRTYPE
        new_file.name = "system/lib/modules"
        new_file.mode = 0o755
        new_file.mtime = int(time.strftime("%s", time.localtime()))
        new_file.uname = "root"
        new_file.gname = "root"
        target_tarball.addfile(new_file)

        source_tarball.close()
        target_tarball.close()

        # Create the pool if it doesn't exist
        if not os.path.exists(os.path.join(conf.publish_path, "pool")):
            os.makedirs(os.path.join(conf.publish_path, "pool"))

        # Compress the target tarball and sign it
        tools.xz_compress(os.path.join(temp_dir, "target.tar"), path)
        gpg.sign_file(conf, "image-signing", path)

        # Generate the metadata file
        metadata = {}
        metadata['generator'] = "cdimage-ubuntu"
        metadata['version'] = version
        metadata['version_detail'] = version_detail
        metadata['series'] = series
        metadata['rootfs_path'] = rootfs_path
        metadata['rootfs_checksum'] = rootfs_hash

        with open(path.replace(".tar.xz", ".json"), "w+") as fd:
            fd.write("%s\n" % json.dumps(metadata, sort_keys=True,
                                         indent=4, separators=(',', ': ')))
        gpg.sign_file(conf, "image-signing", path.replace(".tar.xz", ".json"))

        # Cleanup
        shutil.rmtree(temp_dir)

        environment['version_detail'].append(version_detail)
        return path

    return None


def generate_file_http(conf, arguments, environment):
    """
        Grab, cache and returns a file using http/https.
    """

    # We need at least a URL
    if len(arguments) == 0:
        return None

    # Read the arguments
    url = arguments[0]

    options = {}
    if len(arguments) > 1:
        options = unpack_arguments(arguments[1])

    path = None
    version = None

    if "http_%s" % url in CACHE:
        version = CACHE['http_%s' % url]

    # Get the version/build number
    if "monitor" in options or version:
        if not version:
            # Grab the current version number
            old_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(5)
            try:
                version = urlopen(options['monitor']).read().strip()
            except socket.timeout:
                return None
            except IOError:
                return None
            socket.setdefaulttimeout(old_timeout)

            # Validate the version number
            if not version or len(version.split("\n")) > 1:
                return None

            # Push the result in the cache
            CACHE['http_%s' % url] = version

        # Set version_detail
        version_detail = "%s=%s" % (options.get("name", "http"), version)

        # FIXME: can be dropped once all the non-hased tarballs are gone
        old_path = os.path.realpath(os.path.join(conf.publish_path, "pool",
                                                 "%s-%s.tar.xz" %
                                                 (options.get("name", "http"),
                                                  version)))
        if os.path.exists(old_path):
            # Get the real version number (in case it got copied)
            if os.path.exists(old_path.replace(".tar.xz", ".json")):
                with open(old_path.replace(".tar.xz", ".json"), "r") as fd:
                    metadata = json.loads(fd.read())

                if "version_detail" in metadata:
                    version_detail = metadata['version_detail']

            environment['version_detail'].append(version_detail)
            return old_path

        # Build the path, hasing together the URL and version
        hash_string = "%s:%s" % (url, version)
        global_hash = sha256(hash_string.encode('utf-8')).hexdigest()
        path = os.path.realpath(os.path.join(conf.publish_path, "pool",
                                             "%s-%s.tar.xz" %
                                             (options.get("name", "http"),
                                              global_hash)))

        # Return pre-existing entries
        if os.path.exists(path):
            # Get the real version number (in case it got copied)
            if os.path.exists(path.replace(".tar.xz", ".json")):
                with open(path.replace(".tar.xz", ".json"), "r") as fd:
                    metadata = json.loads(fd.read())

                if "version_detail" in metadata:
                    version_detail = metadata['version_detail']

            environment['version_detail'].append(version_detail)
            return path

    # Grab the real thing
    tempdir = tempfile.mkdtemp()
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(5)
    try:
        urlretrieve(url, os.path.join(tempdir, "download"))
    except socket.timeout:
        shutil.rmtree(tempdir)
        return None
    except IOError:
        shutil.rmtree(tempdir)
        return None
    socket.setdefaulttimeout(old_timeout)

    # Hash it if we don't have a version number
    if not version:
        # Hash the file
        with open(os.path.join(tempdir, "download"), "rb") as fd:
            version = sha256(fd.read()).hexdigest()

        # Set version_detail
        version_detail = "%s=%s" % (options.get("name", "http"), version)

        # Push the result in the cache
        CACHE['http_%s' % url] = version

        # Build the path
        path = os.path.realpath(os.path.join(conf.publish_path, "pool",
                                             "%s-%s.tar.xz" %
                                             (options.get("name", "http"),
                                              version)))
        # Return pre-existing entries
        if os.path.exists(path):
            # Get the real version number (in case it got copied)
            if os.path.exists(path.replace(".tar.xz", ".json")):
                with open(path.replace(".tar.xz", ".json"), "r") as fd:
                    metadata = json.loads(fd.read())

                if "version_detail" in metadata:
                    version_detail = metadata['version_detail']

            environment['version_detail'].append(version_detail)
            shutil.rmtree(tempdir)
            return path

    # Create the pool if it doesn't exist
    if not os.path.exists(os.path.join(conf.publish_path, "pool")):
        os.makedirs(os.path.join(conf.publish_path, "pool"))

    # Move the file to the pool and sign it
    shutil.move(os.path.join(tempdir, "download"), path)
    gpg.sign_file(conf, "image-signing", path)

    # Generate the metadata file
    metadata = {}
    metadata['generator'] = "http"
    metadata['version'] = version
    metadata['version_detail'] = version_detail
    metadata['url'] = url

    with open(path.replace(".tar.xz", ".json"), "w+") as fd:
        fd.write("%s\n" % json.dumps(metadata, sort_keys=True,
                                     indent=4, separators=(',', ': ')))
    gpg.sign_file(conf, "image-signing", path.replace(".tar.xz", ".json"))

    # Cleanup
    shutil.rmtree(tempdir)

    environment['version_detail'].append(version_detail)
    return path


def generate_file_keyring(conf, arguments, environment):
    """
        Generate a keyring tarball or return a pre-existing one.
    """

    # Don't generate keyring tarballs when nothing changed
    if len(environment['new_files']) == 0:
        return None

    # We need a keyring name
    if len(arguments) == 0:
        return None

    # Read the arguments
    keyring_name = arguments[0]
    keyring_path = os.path.join(conf.gpg_keyring_path, keyring_name)

    # Fail on missing keyring
    if not os.path.exists("%s.tar.xz" % keyring_path) or \
            not os.path.exists("%s.tar.xz.asc" % keyring_path):
        return None

    with open("%s.tar.xz" % keyring_path, "rb") as fd:
        hash_tarball = sha256(fd.read()).hexdigest()

    with open("%s.tar.xz.asc" % keyring_path, "rb") as fd:
        hash_signature = sha256(fd.read()).hexdigest()

    hash_string = "%s/%s" % (hash_tarball, hash_signature)
    global_hash = sha256(hash_string.encode('utf-8')).hexdigest()

    # Build the path
    path = os.path.realpath(os.path.join(conf.publish_path, "pool",
                                         "keyring-%s.tar.xz" %
                                         global_hash))

    # Set the version_detail string
    environment['version_detail'].append("keyring=%s" % keyring_name)

    # Don't bother re-generating a file if it already exists
    if os.path.exists(path):
        return path

    # Create temporary directory
    tempdir = tempfile.mkdtemp()

    # Generate the tarball
    tarball = tarfile.open(os.path.join(tempdir, "output.tar"), "w:")
    tarball.add("%s.tar.xz" % keyring_path,
                arcname="/system/etc/system-image/archive-master.tar.xz",
                filter=root_ownership)
    tarball.add("%s.tar.xz.asc" % keyring_path,
                arcname="/system/etc/system-image/archive-master.tar.xz.asc",
                filter=root_ownership)
    tarball.close()

    # Create the pool if it doesn't exist
    if not os.path.exists(os.path.join(conf.publish_path, "pool")):
        os.makedirs(os.path.join(conf.publish_path, "pool"))

    # Compress and sign it
    tools.xz_compress(os.path.join(tempdir, "output.tar"), path)
    gpg.sign_file(conf, "image-signing", path)

    # Generate the metadata file
    metadata = {}
    metadata['generator'] = "keyring"
    metadata['version'] = global_hash
    metadata['version_detail'] = "keyring=%s" % keyring_name
    metadata['path'] = keyring_path

    with open(path.replace(".tar.xz", ".json"), "w+") as fd:
        fd.write("%s\n" % json.dumps(metadata, sort_keys=True,
                                     indent=4, separators=(',', ': ')))
    gpg.sign_file(conf, "image-signing", path.replace(".tar.xz", ".json"))

    # Cleanup
    shutil.rmtree(tempdir)

    return path


def generate_file_remote_system_image(conf, arguments, environment):
    """
        Import files from a remote system-image server
    """

    # We need at least a channel name and a file prefix
    if len(arguments) < 3:
        return None

    # Read the arguments
    base_url = arguments[0]
    channel_name = arguments[1]
    prefix = arguments[2]

    options = {}
    if len(arguments) > 3:
        options = unpack_arguments(arguments[3])

    device_name = environment['device_name']
    if 'device' in options:
        device_name = options['device']

    # Fetch and validate the remote channels.json
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(5)
    try:
        channel_json = json.loads(urlopen("%s/channels.json" %
                                          base_url).read().decode().strip())
    except socket.timeout:
        return None
    except IOError:
        return None
    socket.setdefaulttimeout(old_timeout)

    if channel_name not in channel_json:
        return None

    if "devices" not in channel_json[channel_name]:
        return None

    if device_name not in channel_json[channel_name]['devices']:
        return None

    if "index" not in (channel_json[channel_name]['devices']
                       [device_name]):
        return None

    index_url = "%s/%s" % (base_url, channel_json[channel_name]['devices']
                           [device_name]['index'])

    # Fetch and validate the remote index.json
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(5)
    try:
        index_json = json.loads(urlopen(index_url).read().decode())
    except socket.timeout:
        return None
    except IOError:
        return None
    socket.setdefaulttimeout(old_timeout)

    # Grab the list of full images
    full_images = sorted([image for image in index_json['images']
                          if image['type'] == "full"],
                         key=lambda image: image['version'])

    # No images
    if not full_images:
        return None

    # Found an image, so let's try to find a match
    for file_entry in full_images[-1]['files']:
        file_name = file_entry['path'].split("/")[-1]
        file_prefix = file_name.rsplit("-", 1)[0]
        if file_prefix == prefix:
            path = os.path.realpath("%s/%s" % (conf.publish_path,
                                               file_entry['path']))
            if os.path.exists(path):
                return path

            # Create the target if needed
            if not os.path.exists(os.path.dirname(path)):
                os.makedirs(os.path.dirname(path))

            # Grab the file
            file_url = "%s/%s" % (base_url, file_entry['path'])
            socket.setdefaulttimeout(5)
            try:
                urlretrieve(file_url, path)
            except socket.timeout:
                if os.path.exists(path):
                    os.remove(path)
                return None
            except IOError:
                if os.path.exists(path):
                    os.remove(path)
                return None
            socket.setdefaulttimeout(old_timeout)

            if "keyring" in options:
                if not tools.repack_recovery_keyring(conf, path,
                                                     options['keyring']):
                    if os.path.exists(path):
                        os.remove(path)
                    return None

            gpg.sign_file(conf, "image-signing", path)

            # Attempt to grab an associated json
            socket.setdefaulttimeout(5)
            json_path = path.replace(".tar.xz", ".json")
            json_url = file_url.replace(".tar.xz", ".json")
            try:
                urlretrieve(json_url, json_path),
            except socket.timeout:
                if os.path.exists(json_path):
                    os.remove(json_path)
            except IOError:
                if os.path.exists(json_path):
                    os.remove(json_path)
            socket.setdefaulttimeout(old_timeout)

            if os.path.exists(json_path):
                gpg.sign_file(conf, "image-signing", json_path)
                with open(json_path, "r") as fd:
                    metadata = json.loads(fd.read())

                if "version_detail" in metadata:
                    environment['version_detail'].append(
                        metadata['version_detail'])

            return path

    return None


def generate_file_system_image(conf, arguments, environment):
    """
        Copy a file from another channel.
    """

    # We need at least a channel name and a file prefix
    if len(arguments) < 2:
        return None

    # Read the arguments
    channel_name = arguments[0]
    prefix = arguments[1]

    # Run some checks
    pub = tree.Tree(conf)
    if channel_name not in pub.list_channels():
        return None

    if (not environment['device_name'] in
            pub.list_channels()[channel_name]['devices']):
        return None

    # Try to find the file
    device = pub.get_device(channel_name, environment['device_name'])

    full_images = sorted([image for image in device.list_images()
                          if image['type'] == "full"],
                         key=lambda image: image['version'])

    # No images
    if not full_images:
        return None

    # Found an image, so let's try to find a match
    for file_entry in full_images[-1]['files']:
        file_name = file_entry['path'].split("/")[-1]
        file_prefix = file_name.rsplit("-", 1)[0]
        if file_prefix == prefix:
            path = os.path.realpath("%s/%s" % (conf.publish_path,
                                               file_entry['path']))

            if os.path.exists(path.replace(".tar.xz", ".json")):
                with open(path.replace(".tar.xz", ".json"), "r") as fd:
                    metadata = json.loads(fd.read())

                if "version_detail" in metadata:
                    environment['version_detail'].append(
                        metadata['version_detail'])

            return path

    return None


def generate_file_version(conf, arguments, environment):
    """
        Generate a version tarball or return a pre-existing one.
    """

    # Don't generate version tarballs when nothing changed
    if len(environment['new_files']) == 0:
        return None

    path = os.path.realpath(os.path.join(environment['device'].path,
                            "version-%s.tar.xz" % environment['version']))

    # Set the version_detail string
    environment['version_detail'].append("version=%s" % environment['version'])

    # Don't bother re-generating a file if it already exists
    if os.path.exists(path):
        return path

    # Generate version_detail
    version_detail = ",".join(environment['version_detail'])

    # Create temporary directory
    tempdir = tempfile.mkdtemp()

    # Generate the tarball
    tools.generate_version_tarball(
        conf, environment['channel_name'], environment['device_name'],
        str(environment['version']),
        os.path.join(tempdir, "version"), version_detail=version_detail)

    # Create the pool if it doesn't exist
    if not os.path.exists(os.path.join(environment['device'].path)):
        os.makedirs(os.path.join(environment['device'].path))

    # Compress and sign it
    tools.xz_compress(os.path.join(tempdir, "version"), path)
    gpg.sign_file(conf, "image-signing", path)

    # Generate the metadata file
    metadata = {}
    metadata['generator'] = "version"
    metadata['version'] = environment['version']
    metadata['version_detail'] = "version=%s" % environment['version']
    metadata['channel.ini'] = {}
    metadata['channel.ini']['channel'] = environment['channel_name']
    metadata['channel.ini']['device'] = environment['device_name']
    metadata['channel.ini']['version'] = str(environment['version'])
    metadata['channel.ini']['version_detail'] = version_detail

    with open(path.replace(".tar.xz", ".json"), "w+") as fd:
        fd.write("%s\n" % json.dumps(metadata, sort_keys=True,
                                     indent=4, separators=(',', ': ')))
    gpg.sign_file(conf, "image-signing", path.replace(".tar.xz", ".json"))

    # Cleanup
    shutil.rmtree(tempdir)

    return path
