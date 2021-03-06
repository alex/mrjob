# -*- coding: utf-8 -*-
# Copyright 2015 Yelp and Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import posixpath
import re
from logging import getLogger

from mrjob.parse import is_s3_uri
from mrjob.util import file_ext

# old code, currently used by EMR log parsing

# relative path to look for logs in
_LOG_TYPE_TO_RELATIVE_PATH = dict(
    all='',
    job='history',
    node='',  # do these exist on Hadoop?
    step='steps',
    task='userlogs',
)

# alternate relative path for logs on S3 (EMR)
_S3_LOG_TYPE_TO_RELATIVE_PATH = dict(
    all='',
    job='jobs',
    node='node',
    step='steps',
    task='task-attempts',
)

# if we SSH into a node, default place to look for Hadoop logs
_DEFAULT_NODE_LOG_PATH = '/mnt/var/log/hadoop'

# match a job log path
# TODO: is this really a timestamp on YARN?
_JOB_LOG_PATH_RE = re.compile(
    r'^.*?'     # sometimes there is a number at the beginning, and the
                # containing directory can be almost anything.
    r'job_(?P<timestamp>\d+)_(?P<step_num>\d+)'  # oh look, meaningful data!
    r'([_-]\d+)?'  # sometimes there is a number here.
    r'[_-](?P<user>.*?)[_-]streamjob(\d+).jar'
    r'(-[A-Za-z0-9-]+\.jhist)?(\.gz)?'  # this happens on YARN
    r'$')

# match a node log path
# TODO: update this to match YARN too (use "application")
# TODO: actually, that may be more like the task attempt logs?
# TODO: not really sure what node logs are for
_NODE_LOG_PATH_RE = re.compile(
    r'^.*?/hadoop-hadoop-(jobtracker|namenode).*.out$')

# match a step log path (including s-AAAAAAA step IDs on EMR)
_STEP_LOG_PATH_RE = re.compile(
    r'^.*/((?P<step_num>\d+)|(?P<step_id>s-[A-Z0-9]+))'
    r'/(?P<stream>syslog|stderr)(\.gz)?$')

# match a task attempt log path
# TODO: this is different on 3.x AMIs (and maybe YARN)
_TASK_LOG_PATH_RE = re.compile(
    r'^.*/(?:attempt|container)_'                        # attempt_
    r'(?P<timestamp>\d+)_'                               # 201203222119_
    r'(?P<step_num>\d+)_'                                # 0001_
    r'(?:(?P<task_type>\w)|(?P<yarn_attempt_num>\d+))_'  # m_
    r'(?P<task_num>\d+)'                                 # 000000_
    r'(?:_(?P<attempt_num>\d+))?/'                       # 3/
    r'(?P<stream>stderr|syslog)(\.gz)?$')                # stderr

# map from log type to a regex matching it
_LOG_TYPE_TO_RE = dict(
    all=re.compile(r'.*'),
    job=_JOB_LOG_PATH_RE,
    node=_NODE_LOG_PATH_RE,
    step=_STEP_LOG_PATH_RE,
    task=_TASK_LOG_PATH_RE,
)

# where to look for logs when SSHing in
# (either 'master', 'slaves', or both)
_SSH_LOG_TYPE_TO_LOCATIONS = dict(
    all=['master', 'slaves'],
    job=['master'],
    node=['slaves'],  # TODO: why not master?
    step=['master'],
    task=['master', 'slaves'],
)

log = getLogger(__name__)


def ls_logs(fs, log_type,
            log_dir=None,
            node_log_path=None,
            ssh_host=None,
            step_nums=None,
            step_num_to_id=None):
    """List all paths of logs of the given type.

    Returns a list, sorted so that the most important logs for determining
    cause of failure (basically, the earliest ones) come first.

    We try not to return duplicate logs; if we can successfully fetch
    logs via SSH, we don't attempt to also fetch them from *log_dir*.

    :param fs: a `~mrjob.fs.base.FileSystem` object
    :param log_type: one of ``'job'``, ``'node'``, ``'step'`` or
                     ``'task'``
    :param log_dir: s3:// or hdfs:// URI to fetch logs from
    :param node_log_path: where on a node that we SSH into to look
                          for logs (defaults to
    :param ssh_host: hostname of master node, to SSH into
    :param step_nums: set of step nums to include
    :param step_num_to_id: map from step number to step ID (for EMR)

    Everything except fs and log_type should be a keyword argument.
    """
    log_path_re = _LOG_TYPE_TO_RE.get(log_type)
    if log_path_re is None:
        return None

    # generate list of valid step_ids
    step_ids = None
    if step_nums is not None:
        if step_num_to_id is None:
            step_ids = set()
        else:
            step_ids = set(step_num_to_id[step_num]
                           for step_num in step_nums
                           if step_num in step_num_to_id)

    # try each place we can get logs, one at time (if applicable, first SSH,
    # and then S3/HDFS). Stop once we get any logs at all, so that we
    # don't fetch duplicates.
    for log_subdirs in _candidate_log_subdirs(
            fs, log_type, log_dir=log_dir, node_log_path=node_log_path,
            ssh_host=ssh_host):

        log_paths = []

        for log_subdir in log_subdirs:
            log.info('looking for %s logs in %s' % (log_type, log_subdir))
            try:
                for log_path in fs.ls(log_subdir):
                    m = log_path_re.match(log_path)
                    if not m:
                        continue

                    m_groups = m.groupdict()

                    # filter by step_num
                    if (step_nums is not None and
                            m_groups.get('step_num') and
                            int(m_groups['step_num']) not in step_nums):
                        continue

                    # filter by step_id
                    if (step_ids is not None and
                            m_groups.get('step_id') and
                            m_groups['step_id'] not in step_ids):
                        continue

                    # it matches!
                    log_paths.append(log_path)
            except IOError as e:
                # problem with this log path, try another one
                log.warning("couldn't ls() %s: %r" % (log_subdir, e))

        if log_paths:
            return _sorted_log_paths(log_paths, log_path_re,
                                     step_num_to_id=step_num_to_id)

    # couldn't find anything
    return []


def _candidate_log_subdirs(fs, log_type, log_dir, node_log_path, ssh_host):
    """Yield lists of subdirectories to look for logs in.

    Currently, this means first SSH (if *ssh_host* is set), and then *log_dir*
    (if set).
    """
    # first, try SSH (most up-to-date)
    if ssh_host:
        yield _ssh_log_subdirs(
            fs, log_type, node_log_path=node_log_path, ssh_host=ssh_host)

    # then try the log directory
    if log_dir:
        if is_s3_uri(log_dir):
            relative_path = _S3_LOG_TYPE_TO_RELATIVE_PATH.get(log_type)
        else:
            relative_path = _LOG_TYPE_TO_RELATIVE_PATH.get(log_type)

        if relative_path is not None:
            yield [posixpath.join(log_dir, relative_path, '')]


def _ssh_log_subdirs(fs, log_type, ssh_host, node_log_path):
    """Return a list of SSH URIs where we can look for logs. Depending
    on the log type, we may want to fetch logs from slave nodes
    as well, which involves requesting their list of hostnames.
    """
    # bail out if fs doesn't support it (fetching slave addresses would fail)
    if not (ssh_host and fs.can_handle_path('ssh://%s/' % ssh_host)):
        return []

    # fix/check node_log_path
    if node_log_path is None:
        node_log_path = _DEFAULT_NODE_LOG_PATH

    if not node_log_path.startswith('/'):
        raise ValueError('node_log_path must start with /')

    # get relative path
    relative_log_path = _LOG_TYPE_TO_RELATIVE_PATH.get(log_type)
    if relative_log_path is None:
        return []

    # join node (root) log path and relative path, with trailing slash
    log_path = posixpath.join(node_log_path, relative_log_path, '')

    hosts = []

    log_locations = _SSH_LOG_TYPE_TO_LOCATIONS.get(log_type, ())

    if 'master' in log_locations:
        hosts.append(ssh_host)

    if 'slaves' in log_locations:
        try:
            slave_hosts = fs.ssh_slave_hosts(ssh_host)
        except IOError:
            log.warning('Could not get slave addresses for %s' % ssh_host)
        else:
            for slave_host in slave_hosts:
                hosts.append(ssh_host + '!' + slave_host)

    return ['ssh://%s%s' % (host, log_path) for host in hosts]


def _sorted_log_paths(log_paths, log_path_re, step_num_to_id=None):
    """Order log paths so that the ones most useful for diagnosing
    failure (usually, the latest ones) come first."""
    step_id_to_num = dict((v, k) for k, v in (step_num_to_id or {}).items())

    def sort_key_for_m_group(m_groups, group_name):
        """The sort key we want to use for various groups
        the regex can match."""
        group_value = m_groups.get(group_name)

        if group_name == 'step_id':
            return step_id_to_num.get(group_value, float('inf'))

        elif group_name.endswith('_num'):  # step_num, attempt_num
            if group_value is None:
                return float('inf')
            else:
                return int(group_value)

        elif group_name == 'stream':
            return (group_value == 'stderr', group_value or '')

        else:
            return group_value or ''

    def sort_key(log_path):
        m = log_path_re.match(log_path)
        if not m:
            return []  # this shouldn't happen, see ls_logs(), above

        m_groups = m.groupdict()
        return [sort_key_for_m_group(m_groups, name) for name in
                ('step_num', 'step_id', 'task_type', 'attempt_num',
                 'yarn_attempt_num', 'stream', 'task_num',
                 'timestamp')] + [log_path]

    return sorted(log_paths, key=sort_key, reverse=True)



# Hadoop logging stuff; this is the new hotness

# what syslog paths look like on YARN
_YARN_TASK_SYSLOG_RE = re.compile(
    r'^(?P<prefix>.*?/)'
    r'(?P<application_id>application_\d+_\d{4})/'
    r'(?P<container_id>container(_\d+)+)/'
    r'syslog(?P<suffix>\.\w+)?')

# what syslog paths look like pre-YARN
_PRE_YARN_TASK_SYSLOG_RE = re.compile(
    r'^(?P<prefix>.*?/)'
    r'attempt_(?P<timestamp>\d+)_(?P<step_num>\d+)_'
    r'(?P<task_type>[mr])_(?P<task_num>\d+)_'
    r'(?P<attempt_num>\d+)/'
    r'syslog(?P<suffix>\.\w+)?')

# what job history (e.g. counters) look like on either YARN or pre-YARN.
# YARN uses - instead of _ to separate fields. This should work for
# non-streaming jars as well.
_JOB_HISTORY_RE = re.compile(
    r'^(?P<prefix>.*?/)'
    r'(?P<job_id>job_\d+_\d{4})'
    r'[_-]\d+[_-]hadoop[_-]\S*$')

def _ls_logs(fs, log_dir):
    """ls() the given directory, but log a warning on IOError."""
    try:
        for path in fs.ls(log_dir):
            yield path
    except IOError as e:
        log.warning("couldn't ls() %s: %r" % (log_dir, e))


def _ls_yarn_task_syslogs(fs, log_dirs, application_id=None):
    """List all task syslogs in the given directories, in reverse order, so
    we can find where the job failed. Optionally filter by
    *application_id*.

    Once we find a log dir with *any* syslogs in it, we won't search
    subsequent directories (since these will probably have copies of
    the same logs).

    This function isn't sensitive about how far up the directory tree
    your log dir is: you can search in *log_dir*, or *log_dir*/userlogs/,
    or *log_dir*/userlogs/*application_id* (or /, but don't do that).
    """
    key_func = lambda path: _yarn_task_syslog_sort_key(
        path, application_id=application_id)

    return _ls_syslogs_helper(fs, log_dirs, key_func)


def _ls_pre_yarn_task_syslogs(fs, log_dirs, job_id=None):
    """Like _ls_yarn_task_syslogs(), but for pre-YARN logs"""
    key_func = lambda path: _pre_yarn_task_syslog_sort_key(
        path, job_id=job_id)

    return _ls_syslogs_helper(fs, log_dirs, key_func)


def _yarn_task_syslog_sort_key(path, application_id=None):
    """Given the path of a log file, return the sort key
    (basically, chronological order) if it's
    a syslog that we want, and otherwise return None.

    Optionally, specify a single application ID to filter on.
    """
    m = _YARN_TASK_SYSLOG_RE.match(path)
    if not m:
        return None

    if not (application_id is None or
            m.group('application_id') == application_id):
        return None

    return (m.group('application_id'), m.group('container_id'))


def _pre_yarn_task_syslog_sort_key(path, job_id=None):
    """Given the path of a log file, return the sort key
    (basically, chronological order) if it's
    a syslog that we want, and otherwise return None.

    Optionally, specify a single job ID to filter on.
    """
    m = _PRE_YARN_TASK_SYSLOG_RE.match(path)
    if not m:
        return None

    if job_id is not None:
        log_job_id = 'job_%s_%s' % (m.group('timestamp'), m.group('step_num'))
        if log_job_id != job_id:
            return None

    # we'd rather match later attempts than later tasks (failed steps
    # are re-attempted a fixed number of times)
    return (m.group('timestamp'),
            int(m.group('step_num')),
            m.group('task_type'),
            int(m.group('attempt_num')),
            int(m.group('task_num')))


# helper for _ls_yarn_task_syslogs and _ls_pre_yarn_task_syslogs
def _ls_syslogs_helper(fs, log_dirs, key_func):
    if isinstance(log_dirs, str):
        raise TypeError

    path_to_sort_key = {}

    for log_dir in log_dirs:

        for path in _ls_logs(fs, log_dir):
            sort_key = key_func(path)

            if sort_key:
                path_to_sort_key[path] = sort_key

    return sorted(path_to_sort_key, key=lambda k: path_to_sort_key[k],
                  reverse=True)


def _stderr_for_syslog(path):
    """Get the path/uri of the stderr log corresponding to the given syslog.

    If the syslog is gzipped (/path/to/syslog.gz), we'll expect
    stderr to be gzipped too (/path/to/stderr.gz).
    """
    stem, filename = posixpath.split(path)
    return posixpath.join(stem, 'stderr' + file_ext(filename))


def _ls_job_history_logs(fs, log_dirs, job_id=None):
    """Yield paths/uris of all job history files in the given directories,
    optionally filtering by *job_id*.
    """
    if isinstance(log_dirs, str):
        raise TypeError

    for log_dir in log_dirs:
        for path in _ls_logs(fs, log_dir):
            m = _JOB_HISTORY_RE.match(path)
            if not m:
                continue

            if not (job_id is None or m.group('job_id') == job_id):
                continue

            yield path
