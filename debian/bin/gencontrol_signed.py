#!/usr/bin/python3

import json
import os.path
import pathlib
import re
import subprocess
import sys
import tempfile

from debian_linux.config import ConfigCoreDump
from debian_linux.debian import VersionLinux, BinaryPackage
from debian_linux.gencontrol import Gencontrol as Base, \
    iter_flavours, PackagesBundle
from debian_linux.utils import Templates


class Gencontrol(Base):
    def __init__(self, arch):
        super(Gencontrol, self).__init__(
            ConfigCoreDump(fp=open('debian/config.defines.dump', 'rb')),
            Templates(['debian/signing_templates', 'debian/templates']))

        image_binary_version = self.changelog[0].version.complete

        config_entry = self.config[('version',)]
        self.version = VersionLinux(config_entry['source'])

        # Check config version matches changelog version
        assert self.version.complete == re.sub(r'\+b\d+$', r'',
                                               image_binary_version)

        self.abiname = config_entry['abiname']
        self.vars = {
            'upstreamversion': self.version.linux_upstream,
            'version': self.version.linux_version,
            'source_basename': re.sub(r'-[\d.]+$', '',
                                      self.changelog[0].source),
            'source_upstream': self.version.upstream,
            'abiname': self.abiname,
            'imagebinaryversion': image_binary_version,
            'imagesourceversion': self.version.complete,
            'arch': arch,
        }
        self.vars['source_suffix'] = \
            self.changelog[0].source[len(self.vars['source_basename']):]
        self.vars['template'] = \
            'linux-image%(source_suffix)s-%(arch)s-signed-template' % self.vars

        self.package_dir = 'debian/%(template)s' % self.vars
        self.template_top_dir = (self.package_dir
                                 + '/usr/share/code-signing/%(template)s'
                                 % self.vars)
        self.template_debian_dir = (self.template_top_dir
                                    + '/source-template/debian')
        os.makedirs(self.template_debian_dir, exist_ok=True)

        self.image_packages = []

        # We need a separate base dir for now
        self.bundles = {None: PackagesBundle(None, self.templates,
                                             pathlib.Path(self.template_debian_dir))}
        self.packages = self.bundle.packages
        self.makefile = self.bundle.makefile

    def do_main_setup(self, vars, makeflags, extra):
        makeflags['VERSION'] = self.version.linux_version
        makeflags['GENCONTROL_ARGS'] = (
            '-v%(imagebinaryversion)s '
            '-DBuilt-Using="%(source_basename)s%(source_suffix)s (= %(imagesourceversion)s)"' %
            vars)
        makeflags['PACKAGE_VERSION'] = vars['imagebinaryversion']

        if os.getenv('DEBIAN_KERNEL_DISABLE_INSTALLER'):
            if self.changelog[0].distribution == 'UNRELEASED':
                import warnings
                warnings.warn('Disable installer modules on request '
                              '(DEBIAN_KERNEL_DISABLE_INSTALLER set)')
            else:
                raise RuntimeError(
                    'Unable to disable installer modules in release build '
                    '(DEBIAN_KERNEL_DISABLE_INSTALLER set)')
            self.disable_installer = True
        else:
            self.disable_installer = False

    def do_main_packages(self, vars, makeflags, extra):
        # Assume that arch:all packages do not get binNMU'd
        self.packages['source']['Build-Depends'].append(
            'linux-support-%(abiname)s (= %(imagesourceversion)s)' % vars)

    def do_main_recurse(self, vars, makeflags, extra):
        # Each signed source package only covers a single architecture
        self.do_arch(vars['arch'], vars.copy(),
                     makeflags.copy(), extra)

    def do_extra(self):
        pass

    def do_arch_setup(self, vars, makeflags, arch, extra):
        super(Gencontrol, self).do_main_setup(vars, makeflags, extra)

        abiname_part = '-%s' % self.config.merge('abi', arch)['abiname']
        makeflags['ABINAME'] = vars['abiname'] = \
            self.config['version', ]['abiname_base'] + abiname_part

    def do_arch_packages(self, arch, vars, makeflags, extra):
        pass

    def do_featureset_setup(self, vars, makeflags, arch, featureset, extra):
        self.default_flavour = self.config.merge('base', arch, featureset) \
                                          .get('default-flavour')
        if self.default_flavour is not None:
            if featureset != 'none':
                raise RuntimeError("default-flavour set for %s %s,"
                                   " but must only be set for featureset none"
                                   % (arch, featureset))
            if self.default_flavour \
               not in iter_flavours(self.config, arch, featureset):
                raise RuntimeError("default-flavour %s for %s %s does not exist"
                                   % (self.default_flavour, arch, featureset))

        self.quick_flavour = self.config.merge('base', arch, featureset) \
                                        .get('quick-flavour')

    def do_flavour_setup(self, vars, makeflags, arch, featureset, flavour,
                         extra):
        super(Gencontrol, self).do_flavour_setup(vars, makeflags, arch,
                                                 featureset, flavour, extra)

        config_description = self.config.merge('description', arch, featureset,
                                               flavour)
        config_image = self.config.merge('image', arch, featureset, flavour)

        vars['flavour'] = vars['localversion'][1:]
        vars['class'] = config_description['hardware']
        vars['longclass'] = (config_description.get('hardware-long')
                             or vars['class'])

        vars['image-stem'] = config_image.get('install-stem')
        makeflags['IMAGE_INSTALL_STEM'] = vars['image-stem']

    def do_flavour_packages(self, arch, featureset,
                            flavour, vars, makeflags, extra):
        ruleid = (arch, featureset, flavour, 'real')

        config_build = self.config.merge('build', arch, featureset, flavour)
        config_entry_packages = self.config.merge('packages', arch, featureset, flavour)
        if not config_build.get('signed-code', False):
            return

        # In a quick build, only build the quick flavour (if any).
        if 'pkg.linux.quick' in \
           os.environ.get('DEB_BUILD_PROFILES', '').split() \
           and flavour != self.quick_flavour:
            return

        image_suffix = '%(abiname)s%(localversion)s' % vars
        image_package_name = 'linux-image-%s-unsigned' % image_suffix

        # Verify that this flavour is configured to support Secure Boot,
        # and get the trusted certificates filename.
        with open('debian/%s/boot/config-%s' %
                  (image_package_name, image_suffix)) as f:
            kconfig = f.readlines()
        assert 'CONFIG_EFI_STUB=y\n' in kconfig
        assert 'CONFIG_LOCK_DOWN_IN_EFI_SECURE_BOOT=y\n' in kconfig
        self.image_packages.append((image_suffix, image_package_name))

        self.packages['source']['Build-Depends'].append(
            image_package_name
            + ' (= %(imagebinaryversion)s) [%(arch)s]' % vars)

        packages_own = (
            self.bundle.add('image',
                            ruleid, makeflags, vars, arch=arch)
        )

        if self.config.merge('packages').get('meta', True):
            packages_meta = (
                self.bundle.add('image.meta', ruleid, makeflags, vars, arch=arch)
            )
            assert len(packages_meta) == 1
            packages_meta += (
                self.bundle.add('headers.meta', ruleid, makeflags, vars, arch=arch)
            )
            assert len(packages_meta) == 2

            # Don't pretend to support build-profiles
            for package in packages_meta:
                del package['Build-Profiles']

            if flavour == self.default_flavour \
               and not self.vars['source_suffix']:
                packages_meta[0].setdefault('Provides') \
                                .append('linux-image-generic')
                packages_meta[1].setdefault('Provides') \
                                .append('linux-headers-generic')

            packages_own.extend(packages_meta)

        if not self.disable_installer and config_entry_packages.get('installer'):
            with tempfile.TemporaryDirectory(prefix='linux-gencontrol') as config_dir:
                base_path = pathlib.Path('debian/installer').absolute()
                config_path = pathlib.Path(config_dir)
                (config_path / 'modules').symlink_to(base_path / 'modules')
                (config_path / 'package-list').symlink_to(base_path / 'package-list')

                with (config_path / 'kernel-versions').open('w') as versions:
                    versions.write(f'{arch} - {vars["flavour"]} - - -\n')

                # Add udebs using kernel-wedge
                kw_env = os.environ.copy()
                kw_env['KW_DEFCONFIG_DIR'] = config_dir
                kw_env['KW_CONFIG_DIR'] = config_dir
                kw_proc = subprocess.Popen(
                    ['kernel-wedge', 'gen-control', vars['abiname']],
                    stdout=subprocess.PIPE,
                    text=True,
                    env=kw_env)
                udeb_packages = BinaryPackage.read_rfc822(kw_proc.stdout)
                kw_proc.wait()
                if kw_proc.returncode != 0:
                    raise RuntimeError('kernel-wedge exited with code %d' %
                                       kw_proc.returncode)

            for package in udeb_packages:
                # kernel-wedge currently chokes on Build-Profiles so add it now
                package['Build-Profiles'] = (
                    '<!noudeb !pkg.linux.nokernel !pkg.linux.quick>')
                package.meta['rules-target'] = 'installer'

            makeflags_local = makeflags.copy()
            makeflags_local['IMAGE_PACKAGE_NAME'] = udeb_packages[0]['Package']

            self.bundle.add_packages(
                udeb_packages,
                (arch, featureset, flavour, 'real'),
                makeflags_local, arch=arch,
            )

    def write(self):
        self.bundle.extract_makefile()
        self.write_changelog()
        self.write_control(name=(self.template_debian_dir + '/control'))
        self.write_makefile(name=(self.template_debian_dir + '/rules.gen'))
        self.write_files_json()
        self.write_source_lintian_overrides()

    def write_changelog(self):
        # Copy the linux changelog, but:
        # * Change the source package name and version
        # * Insert a line to refer to refer to the linux source version
        vars = self.vars.copy()
        vars['source'] = self.changelog[0].source
        vars['distribution'] = self.changelog[0].distribution
        vars['urgency'] = self.changelog[0].urgency
        vars['signedsourceversion'] = \
            re.sub(r'\+b(\d+)$', r'.b\1',
                   re.sub(r'-', r'+', vars['imagebinaryversion']))

        with open(self.template_debian_dir + '/changelog', 'w',
                  encoding='utf-8') as f:
            f.write(self.substitute('''\
linux-signed@source_suffix@-@arch@ (@signedsourceversion@) @distribution@; urgency=@urgency@

  * Sign kernel from @source@ @imagebinaryversion@

''',
                                    vars))

            with open('debian/changelog', 'r', encoding='utf-8') \
                 as changelog_in:
                # Ignore first two header lines
                changelog_in.readline()
                changelog_in.readline()

                for d in changelog_in.read():
                    f.write(d)

    def write_files_json(self):
        all_files = {'packages': {}}

        for image_suffix, image_package_name in self.image_packages:
            package_files = []
            package_files.append({'sig_type': 'efi',
                                  'file': 'boot/vmlinuz-%s' % image_suffix})
            all_files['packages'][image_package_name] = {
                'trusted_certs': [],
                'files': package_files
            }

        with open(self.template_top_dir + '/files.json', 'w') as f:
            json.dump(all_files, f)

    def write_source_lintian_overrides(self):
        os.makedirs(os.path.join(self.template_debian_dir, 'source'),
                    exist_ok=True)
        with open(os.path.join(self.template_debian_dir,
                               'source/lintian-overrides'), 'w') as f:
            f.write(self.substitute(self.templates.get('source.lintian-overrides'),
                                    self.vars))


if __name__ == '__main__':
    Gencontrol(sys.argv[1])()
