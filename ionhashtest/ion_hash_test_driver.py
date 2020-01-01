# Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at:
#
#    http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS
# OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the
# License.

"""Cross-implementation test driver.

Usage:
    ion_hash_test_driver.py [--implementation <description>]... [--ion-hash-test <description>]...
                            [--local-only] [--git <path>] [--output-dir <dir>] [--results-file <file>]
                            [<test_file>]...
    ion_hash_test_driver.py (--list)
    ion_hash_test_driver.py (-h | --help)

Options:
    --git <path>                        Path to the git executable.

    -h, --help                          Show this screen.

    -i, --implementation <description>  Test an additional implementation specified by a description of the form
                                        name,location,revision. Name must match one of the names returned by `--list`.
                                        Location may be a local path or a URL. Revision is optional, may be either a
                                        branch name or commit hash, and defaults to `master`.

    -I, --ion-hash-test <description>   Override the default ion-hash-test location by providing a description of the form
                                        location,revision. Location may be a local path or a URL. Revision is optional,
                                        may be either a branch name or commit hash, and defaults to `master`.

    -l, --list                          List the implementations that can be built by this tool.

    -L, --local-only                    Test using only local implementations specified by `--implementation`.

    -o, --output-dir <dir>              Root directory for all of this command's output. [default: .]

    -r, --results-file <file>           Path to the results output file. By default, this will be placed in a file named
                                        `ion-test-driver-results.ion` under the directory specified by the
                                        `--output-dir` option.


"""
import os
import shutil
from io import FileIO
from subprocess import check_call, check_output, Popen, PIPE
import six
from amazon.ion import simpleion
from amazon.ion.core import IonType
from amazon.ion.simple_types import IonPySymbol, IonPyList
from amazon.ion.symbols import SymbolToken
from amazon.ion.util import Enum
from docopt import docopt

from ionhashtest.config import TOOL_DEPENDENCIES, ION_BUILDS, ION_IMPLEMENTATIONS, ION_HASH_TEST_SOURCE,\
    RESULTS_FILE_DEFAULT
from ionhashtest.util import COMMAND_SHELL, log_call
from ionhashtest.test_data import generate_tests


ION_SUFFIX_TEXT = '.ion'
ION_SUFFIX_BINARY = '.10n'


def check_tool_dependencies(args):
    """
    Verifies that all dependencies declared by `TOOL_DEPENDENCIES` are executable.
    :param args: If any of the tool dependencies are present, uses the value to override the default location.
    """
    names = TOOL_DEPENDENCIES.keys()
    for name in names:
        path = args['--' + name]
        if path:
            TOOL_DEPENDENCIES[name] = path
    for name, path in six.iteritems(TOOL_DEPENDENCIES):
        try:
            # NOTE: if a tool dependency is added that doesn't have a `--help` command, the logic should be generalized
            # to call a tool-specific command to test the existence of the executable. This should be a command that
            # always returns zero.
            no_output = open(os.devnull, 'w')
            check_call([path, '--help'], stdout=no_output, shell=COMMAND_SHELL)
        except:
            raise ValueError(name + " not found. Try specifying its location using --" + name + ".")
        finally:
            no_output.close()


class IonResource:
    def __init__(self, output_root, name, location, revision):
        """
        Provides the installation logic for a resource required to run the tests.

        :param output_root: Root directory for the build output.
        :param name: Name of the resource.
        :param location: Location from which to git clone the resource.
        :param revision: Git revision of the resource.
        """
        self.__output_root = output_root
        try:
            self._build = ION_BUILDS[name]
        except KeyError:
            raise ValueError('No installer for %s.' % name)
        self._name = name
        self._build_dir = None
        self.__build_log = None
        self.__identifier = None
        self._executable = None
        self.__location = location
        self.__revision = revision

    @property
    def identifier(self):
        if self.__identifier is None:
            raise ValueError('Implementation %s must be installed before receiving an identifier.' % self._name)
        return self.__identifier

    def __git_clone_revision(self):
        # The commit is not yet known; clone into a temporary location to determine the commit and decide whether the
        # code for that revision is already present. If it is, use the existing code, as it may have already been built.
        tmp_dir_root = os.path.abspath((os.path.join(self.__output_root, 'build', 'tmp')))
        try:
            tmp_dir = os.path.abspath(os.path.join(tmp_dir_root, self._name))
            if not os.path.isdir(tmp_dir_root):
                os.makedirs(tmp_dir_root)
            tmp_log = os.path.abspath(os.path.join(tmp_dir_root, 'tmp_log.txt'))
            log_call(tmp_log, (TOOL_DEPENDENCIES['git'], 'clone', '--recurse-submodules', self.__location,
                     tmp_dir))
            os.chdir(tmp_dir)
            if self.__revision is not None:
                log_call(tmp_log, (TOOL_DEPENDENCIES['git'], 'checkout', self.__revision))
                log_call(tmp_log, (TOOL_DEPENDENCIES['git'], 'submodule', 'update', '--init'))
            commit = check_output((TOOL_DEPENDENCIES['git'], 'rev-parse', '--short', 'HEAD')).strip()
            self.__identifier = self._name + '_' + commit.decode()
            self._build_dir = os.path.abspath(os.path.join(self.__output_root, 'build', self.__identifier))
            logs_dir = os.path.abspath(os.path.join(self.__output_root, 'build', 'logs'))
            if not os.path.isdir(logs_dir):
                os.makedirs(logs_dir)
            self.__build_log = os.path.abspath(os.path.join(logs_dir, self.__identifier + '.txt'))
            if not os.path.exists(self._build_dir):
                shutil.move(tmp_log, self.__build_log)  # This build is being used, overwrite an existing log (if any).
                shutil.move(tmp_dir, self._build_dir)
            else:
                print("%s already present. Using existing source." % self._build_dir)
        finally:
            shutil.rmtree(tmp_dir_root)

    def install(self):
        print('Installing %s revision %s.' % (self._name, self.__revision))
        self.__git_clone_revision()
        os.chdir(self._build_dir)
        self._build.install(self.__build_log)
        os.chdir(self.__output_root)
        print('Done installing %s.' % self.identifier)
        return self._build_dir


class IonHashImplementation(IonResource):
    def __init__(self, output_root, name, location, revision):
        """
        An executable `IonResource`; used to represent different Ion implementations.
        """
        super(IonHashImplementation, self).__init__(output_root, name, location, revision)

    def test(self, test_file, algorithm):
        print("Running %s..." % self._name)

        if self._build_dir is None:
            raise ValueError('Implementation %s has not been installed.' % self._name)
        if self._executable is None:
            if self._build.execute is None:
                raise ValueError('Implementation %s is not executable.' % self._name)
            self._executable = os.path.abspath(os.path.join(self._build_dir, self._build.execute))
        if not os.path.isfile(self._executable):
            raise ValueError('Executable for %s does not exist.' % self._name)

        outfile = open(self._name + ".out", "w")
        _, stderr = Popen([self._executable, algorithm, test_file], stdin=PIPE, stdout=outfile, stderr=PIPE).communicate()
        if len(stderr) > 0:
            print(stderr)
        outfile.close()


digest_no_comparison = 0
digest_matches = 0
digest_inconsistencies = 0


def report(impls, test_file):
    report_files = {}
    for impl in impls:
        report_files[impl._name] = open(impl._name + ".out")

    _report = {}

    digest_comparisons = []
    with open(test_file) as tf:
        for line in tf:
            line = line.rstrip()
            compare_test(line, report_files, digest_comparisons)

    for report_file in report_files.values():
        report_file.close()

    _report['digests'] = digest_comparisons

    summary = dict()
    summary['test_count'] = len(digest_comparisons)
    summary['digest_matches'] = digest_matches
    summary['digest_inconsistent'] = digest_inconsistencies
    summary['digest_no_comparison'] = digest_no_comparison

    _report['summary'] = summary
    return _report


def compare_test(line, report_files, digest_comparisons):
    global digest_no_comparison
    global digest_matches
    global digest_inconsistencies

    digests = {}
    for impl_name, report_file in report_files.items():
        digest = report_file.readline().rstrip()
        if digest.startswith("[unable to digest"):
            digests[impl_name] = "[unable to digest]"
        else:
            digests[impl_name] = digest

    digest_set = set(digests.values())

    digest_comparison = {}
    if len(digest_set) == 0:
        digest_comparison['result'] = SymbolToken('no_comparison', None, None)
        digest_no_comparison += 1
    elif len(digest_set) == 1:
        digest_comparison['result'] = SymbolToken('full_match', None, None)
        digest_comparison['digest'] = digest_set.pop()
        digest_matches += 1
    else:
        impl_digests = []
        entry = {}
        for impl_name, digest in digests.items():
            entry[impl_name] = digest
            impl_digests.append(entry)

        digest_comparison['result'] = SymbolToken('inconsistent', None, None)
        digest_comparison['digests'] = impl_digests
        digest_inconsistencies += 1

    digest_comparison['value'] = line

    digest_comparisons.append(digest_comparison)


def tokenize_description(description, has_name):
    """
    Splits comma-separated resource descriptions into tokens.
    :param description: String describing a resource, as described in the ion-hash-test-driver CLI help.
    :param has_name: If True, there may be three tokens, the first of which must be the resource's name. Otherwise,
        there may be a maximum of two tokens, which represent the location and optional revision.
    :return: If `has_name` is True, three components (name, location, revision). Otherwise, two components
        (name, location)
    """
    components = description.split(',')
    max_components = 3
    if not has_name:
        max_components = 2
    if len(components) < max_components:
        revision = 'master'
    else:
        revision = components[max_components - 1]
    if len(components) < max_components - 1:
        raise ValueError("Invalid implementation description.")
    if has_name:
        return components[0], components[max_components - 2], revision
    else:
        return components[max_components - 2], revision


def parse_implementations(descriptions, output_root):
    return [IonHashImplementation(output_root, *tokenize_description(description, has_name=True))
            for description in descriptions]


def ion_hash_test_driver(arguments):
    if arguments['--help']:
        print(__doc__)
    elif arguments['--list']:
        for impl_name in ION_BUILDS:
            if impl_name != 'ion-hash-test':
                print(impl_name)
    else:
        output_root = os.path.abspath(arguments['--output-dir'])
        if not os.path.exists(output_root):
            os.makedirs(output_root)

        implementations = parse_implementations(arguments['--implementation'], output_root)
        if not arguments['--local-only']:
            implementations += parse_implementations(ION_IMPLEMENTATIONS, output_root)
        check_tool_dependencies(arguments)
        for implementation in implementations:
            implementation.install()
        ion_hash_test_source = arguments['--ion-hash-test'] or ION_HASH_TEST_SOURCE
        ion_hash_test_dir = IonResource(
            output_root, 'ion-hash-test', *tokenize_description(ion_hash_test_source, has_name=False)
        ).install()

        '''
        results_root = os.path.join(output_root, 'results')
        results_file = arguments['--results-file'] or RESULTS_FILE_DEFAULT

        test_file_filter = arguments['<test_file>']
        #test_all(implementations, ion_hash_test_dir, test_types, test_file_filter, results_root, results_file)
        '''

        test_file = output_root + "/tests.ion"

        generate_tests(ion_hash_test_dir, test_file)
        test_file = output_root + "/tests.ion.head"

        for impl in implementations:
            impl.test(test_file, "md5")

        the_report = report(implementations, test_file)
        print(simpleion.dumps(the_report, binary=False, indent='  '))

if __name__ == '__main__':
    ion_hash_test_driver(docopt(__doc__))
