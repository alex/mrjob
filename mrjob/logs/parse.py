# Copyright 2015 Yelp
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
import re
from logging import getLogger

from mrjob.logs.util import _sum_counters


# log line format output by YARN hadoop jar command
_HADOOP_LOG_LINE_RE = re.compile(
    r'^(?P<timestamp>.*?)'
    r'\s+(?P<level>[A-Z]+)'
    r'\s+(?P<logger>\S+)'
    r'(\s+\((?P<thread>.*?)\))?'
    r': (?P<message>.*?)$')

# log line format output to Hadoop syslog
_HADOOP_LOG_LINE_ALTERNATE_RE = re.compile(
    r'^(?P<timestamp>.*?)'
    r'\s+(?P<level>[A-Z]+)'
    r'(\s+\[(?P<thread>.*?)\])'
    r'\s+(?P<logger>\S+)'
    r': (?P<message>.*?)$')

_APPLICATION_ID_RE = re.compile(r'\bapplication_\d+_\d{4}\b')

_JOB_ID_RE = re.compile(r'\bjob_\d+_\d{4}\b$')

_OUTPUT_DIR_LINE_RE = re.compile(
    r'^Output( directory)?:'
    r'\s+(?P<output_dir>\S+://\S+)')

# marks start of counters message
_INDENTED_COUNTERS_START_RE = re.compile(r'^Counters: ')

# header for a group of counters
_INDENTED_COUNTER_GROUP_RE = re.compile(r'^(?P<indent>\s+)(?P<group>.*)$')

# line for a counter
_INDENTED_COUNTER_RE = re.compile(
    r'^(?P<indent>\s+)(?P<counter>.*)=(?P<amount>\d+)\s*$')

# message telling us about a (input) split. Looks like this:
#
# Processing split: hdfs://ddf64167693a:9000/path/to/bootstrap.sh:0+335
_YARN_INPUT_SPLIT_RE = re.compile(
    r'^Processing split:\s+(?P<path>.*)'
    r':(?P<start_line>\d+)\+(?P<num_lines>\d+)$')

# this seems to only happen for S3. Not sure if this happens in YARN
_OPENING_FOR_READING_RE = re.compile(
    r"^Opening '(?P<path>.*?)' for reading$")


# start of message telling us about a Java stacktrace on YARN
_YARN_JAVA_EXCEPTION_HEADER_RE = re.compile(
    r'^Exception running child\s?:\s+(?P<exception>.*)$')

# start of message telling us about a Java stacktrace pre-YARN
# (the exception is on the next line)
_PRE_YARN_JAVA_ERROR_HEADER_RE = re.compile(r'^Error running child$')

# start of line telling us about Python exception
_PYTHON_EXCEPTION_HEADER_RE = re.compile(
    r'^Traceback \(most recent call last\):$')

# once we see the exception header, every line starting with whitespace
# is part of the traceback
_PYTHON_TRACEBACK_LINE_RE = re.compile(r'^\s+')

# escape sequence in pre-YARN history file. Characters inside COUNTERS
# fields are double escaped
_PRE_YARN_HISTORY_ESCAPE_RE = re.compile(r'\\(.)')

# capture key-value pairs like JOBNAME="streamjob8025762403845318969\.jar"
_PRE_YARN_HISTORY_KEY_PAIR = re.compile(
    r'(?P<key>\w+)="(?P<escaped_value>(\\.|[^"\\])*)"', re.MULTILINE)

# an entire line in a pre-YARN history file
_PRE_YARN_HISTORY_RECORD = re.compile(
    r'^(?P<type>\w+)'
    r'(?P<key_pairs>( ' + _PRE_YARN_HISTORY_KEY_PAIR.pattern + ')*)'
    r' \.$', re.MULTILINE)

# capture one group of counters
# this looks like: {(group_id)(group_name)[counter][counter]...}
_PRE_YARN_COUNTER_GROUP_RE = re.compile(
    r'{\('
    r'(?P<group_id>(\\.|[^)}\\])*)'
    r'\)\('
    r'(?P<group_name>(\\.|[^)}\\])*)'
    r'\)'
    r'(?P<counter_list_str>\[(\\.|[^}\\])*\])'
    r'}')

# parse a single counter from a counter group (counter_list_str above)
# this looks like: [(counter_id)(counter_name)(amount)]
_PRE_YARN_COUNTER_RE = re.compile(
    r'\[\('
    r'(?P<counter_id>(\\.|[^)\\])*)'
    r'\)\('
    r'(?P<counter_name>(\\.|[^)\\])*)'
    r'\)\('
    r'(?P<amount>\d+)'
    r'\)\]')


log = getLogger(__name__)


def _parse_hadoop_log_lines(lines):
    """Parse lines from a hadoop log into log4j records.

    Yield dictionaries with the following keys:
    timestamp -- unparsed timestamp, e.g. '15/12/07 20:49:28',
        '2015-08-22 00:46:18,411'
    level -- e.g. 'INFO'
    logger -- e.g. 'amazon.emr.metrics.MetricsSaver'
    thread -- e.g. 'main'. May be None
    message -- the actual message. If this is a multi-line message (e.g.
        for counters), the lines will be joined by '\n'

    Trailing \r and \n will be stripped from lines.
    """
    last_record = None

    for line in lines:
        line = line.rstrip('\r\n')

        m = (_HADOOP_LOG_LINE_RE.match(line) or
             _HADOOP_LOG_LINE_ALTERNATE_RE.match(line))

        if m:
            if last_record:
                yield last_record
            last_record = m.groupdict()
        else:
            # add on to previous record
            if last_record:
                last_record['message'] += '\n' + line
            else:
                log.warning('unexpected log line: %s' % line)

    if last_record:
        yield last_record


def _parse_hadoop_streaming_log(lines, record_callback=None):
    """Parse lines from Hadoop's log. This is printed to stderr by the
    Hadoop streaming jar, and to the step's syslog on EMR.

    Returns a dictionary with the following keys:
    application_id: a string like 'application_1449857544442_0002'. Only
        set on YARN
    counters: a map from counter group -> counter -> amount, or None if
        no counters found (only YARN prints counters)
    job_id: a string like 'job_201512112247_0003'. Should always be set
    output_dir: a URI like 'hdfs:///user/hadoop/tmp/my-output-dir'. Should
        always be set on success.

    Optionally, call a callback function (*record_callback*) once for each
    record we parse. Anything we need to know about while the job is running
    (e.g. location of the job tracker, map/reduce %), is better handled by a
    callback.

    This doesn't properly handle the 'Streaming Command Failed!'
    printed by the Hadoop binary (you'll have to pre-filter that).
    """
    application_id = None
    counters = None
    job_id = None
    output_dir = None

    for record in _parse_hadoop_log_lines(lines):
        if record_callback:
            record_callback(record)

        message = record['message']

        if _INDENTED_COUNTERS_START_RE.match(message):
            counters = _parse_indented_counters(message.splitlines())
            continue

        m = _OUTPUT_DIR_LINE_RE.match(message)
        if m:
            output_dir = m.group('output_dir')
            continue

        # grab these wherever we see them
        m = _APPLICATION_ID_RE.search(message)
        if m:
            application_id = m.group()

        m = _JOB_ID_RE.search(message)
        if m:
            job_id = m.group()

    return dict(
        application_id=application_id,
        counters=counters,
        job_id=job_id,
        output_dir=output_dir)


def _parse_indented_counters(lines):
    """Parse counters in the indented format output/logged by the
    Hadoop binary.

    Takes input as lines (should not include log record stuff) and
    returns a map from counter group to counter to amount.

    You usually don't need to call this directly; use
    _parse_hadoop_streaming_log() instead.
    """
    counters = {}  # map group -> counter -> amount
    group = None
    group_indent = None

    for line in lines:
        if not (group is None or group_indent is None):
            m = _INDENTED_COUNTER_RE.match(line)
            if m and len(m.group('indent')) > group_indent:

                counter = m.group('counter')
                amount = int(m.group('amount'))

                counters.setdefault(group, {})
                counters[group][counter] = amount

                continue

        m = _INDENTED_COUNTER_GROUP_RE.match(line)
        if m:
            group = m.group('group')
            group_indent = len(m.group('indent'))

        elif not _INDENTED_COUNTERS_START_RE.match(line):
            log.warning('unexpected counter line: %s' % line)

    return counters


def _parse_task_syslog(lines):
    """Parse out last Java stacktrace (if any) and last split (if any)
    from syslog file.

    Returns a dictionary with the keys 'error' and 'split':

    error: optional (may be None) dictionary with keys:
        exception: string
        stack_trace: [lines]
    split: optional (may be None) dictionary with the keys:
       path: URI of input file
       start_line: optional first line of split (0-indexed)
       num_lines: optional number of lines in split
    """
    # TODO: just make error a string
    result = dict(error=None, split=None)

    for record in _parse_hadoop_log_lines(lines):
        message = record['message']

        m = _OPENING_FOR_READING_RE.match(message)
        if m:
            result['split'] = dict(
                path = m.group('path'),
                start_line=None,
                num_lines=None)
            continue

        # doesn't really hurt to try this on non-YARN logs
        m = _YARN_INPUT_SPLIT_RE.match(message)
        if m:
            result['split'] = dict(
                path=m.group('path'),
                start_line=int(m.group('start_line')),
                num_lines=int(m.group('num_lines')))
            continue

        message_lines = message.splitlines()
        if not message_lines:
            continue

        # TODO: could also generalize this by looking for exception lines
        # ("    at ...(...:###)") rather than a Hadoop-version-specific header
        m = _YARN_JAVA_EXCEPTION_HEADER_RE.match(message_lines[0])
        if m:
            result['error'] = dict(
                exception=m.group('exception'),
                stack_trace=message_lines[1:])
            continue

        if (_PRE_YARN_JAVA_ERROR_HEADER_RE.match(message_lines[0])
            and len(message_lines) > 1):
            result['error'] = dict(
                exception=message_lines[1],
                stack_trace=message_lines[2:])
            continue


    return result


def _parse_python_task_stderr(lines):
    """Parse out the python stacktrace and exception, if any.

    Returns a dictionary with the optional (may be None) key 'error':

    error: dictionary with keys:
        exception: string
        traceback: [lines]
    """
    # stderr can also contain the java Exception (or a warning that no
    # appenders can be found for the logger), but there's not much point
    # in parsing this as we already get it from syslog

    # TODO: handle errors from the setup script (see #1203)

    # TODO: just make error a string, try to find command as well
    result = dict(error=None)

    traceback = None

    for line in lines:
        line = line.rstrip('\r\n')

        if traceback is None:
            if _PYTHON_EXCEPTION_HEADER_RE.match(line):
                traceback = [line]
            # otherwise this line is uninteresting
        else:
            # parsing traceback
            if _PYTHON_TRACEBACK_LINE_RE.match(line):
                traceback.append(line)
            else:
                result['error'] = dict(
                    exception=line,
                    traceback=traceback)
                traceback = None  # done parsing traceback

    return result


def _pre_yarn_history_unescape(s):
    """Un-escape string from a pre-YARN history file."""
    return _PRE_YARN_HISTORY_ESCAPE_RE.sub(r'\1', s)



def _parse_pre_yarn_history_file(lines):
    """Yield records from the given sequence of lines. For example,
    a line like this:

    Task TASKID="task_201512311928_0001_m_000003" \
    TASK_TYPE="MAP" START_TIME="1451590341378" \
    SPLITS="/default-rack/172\.31\.22\.226" .

    into a record like:

    {
        'fields': {'TASKID': 'task_201512311928_0001_m_00000',
                   'TASK_TYPE': 'MAP',
                   'START_TIME': '1451590341378',
                   'SPLITS': '/default-rack/172.31.22.226'},
        'type': 'Task',
        'line_num': 0,
        'num_lines': 1,
    }

    This handles unescaping values, but doesn't do the further
    unescaping needed to process counters. It can also handle multi-line
    records (e.g. for Java stack traces).
    """
    def yield_record_strings(lines):
        record_lines = []
        start_line = 0

        for line_num, line in enumerate(lines):
            record_lines.append(line)
            if line.endswith(' .\n'):
                yield start_line, len(record_lines), ''.join(record_lines)
                record_lines = []
                start_line = line_num + 1

    for start_line, num_lines, record_str in yield_record_strings(lines):
        record_match = _PRE_YARN_HISTORY_RECORD.match(record_str)

        if not record_match:
            continue

        record_type = record_match.group('type')
        key_pairs = record_match.group('key_pairs')

        fields = {}
        for m in _PRE_YARN_HISTORY_KEY_PAIR.finditer(key_pairs):
            key = m.group('key')
            value = _pre_yarn_history_unescape(m.group('escaped_value'))

            fields[key] = value

        yield dict(
            fields=fields,
            num_lines=num_lines,
            start_line=start_line,
            type=record_type,
        )



# TODO: this properly belongs in mrjob.logs.interpret
def _summarize_pre_yarn_history(records, path=None):
    """Collect useful info from a pre-YARN history file. Expects a
    sequence of ``(record_type, {record})`` (see
    :py:func:`_parse_pre_yarn_history_records`).

    This returns a dictionary with the following keys:

    counters: map from group to counter to amount. If job failed, we sum
        counters for succesful tasks
    errors: a list of dictionaries with the keys:
        java_error:
            error: lines of error, as as string
            path: *path*
            start_line: first line of log containing the error (0-indexed)
            num_lines: # of lines of log containing the error
        task_attempt_id: ID of task attempt with this error

    You can optionally pass in *path*, the path of the file, so that it can
    be included in errors.
    """
    # tantalizingly, STATE_STRING contains the split (URI and line numbers)
    # read, but only for successful tasks, which doesn't help with debugging

    task_id_to_counters = {}  # used for successful tasks in failed jobs
    job_counters = None
    errors = []

    for record in records:
        fields = record['fields']

        # if job is successful, we get counters for the entire job at the end
        if record['type'] == 'Job' and 'COUNTERS' in fields:
            job_counters = _parse_pre_yarn_counters(fields['COUNTERS'])

        # otherwise, compile counters for each successful task
        #
        # Note: this apparently records a higher total than the task tracker
        # (possibly some tasks are duplicates?). Couldn't figure out the logic
        # behind this while looking at the history file
        elif (record['type'] == 'Task' and
              'COUNTERS' in fields and 'TASKID' in fields):
            task_id = fields['TASKID']
            counters = _parse_pre_yarn_counters(fields['COUNTERS'])

            task_id_to_counters[task_id] = counters

        elif (record['type'] in ('MapAttempt', 'ReduceAttempt') and
              'TASK_ATTEMPT_ID' in fields and 'ERROR' in fields):
            errors.append(dict(
                java_error=dict(
                    error=fields['ERROR'],
                    path=path,
                    start_line=record['start_line'],
                    num_lines=record['num_lines']),
                task_attempt_id=fields['TASK_ATTEMPT_ID']))

    # if job failed, patch together counters from successful tasks
    if job_counters is None:
        job_counters = _sum_counters(*task_id_to_counters.values())

    return dict(counters=job_counters,
                errors=errors)


def _parse_pre_yarn_counters(counters_str):
    """Parse a COUNTERS field from a pre-YARN history file.

    Returns a map from group to counter to amount.
    """
    counters = {}

    for group_match in _PRE_YARN_COUNTER_GROUP_RE.finditer(counters_str):
        group_name = _pre_yarn_history_unescape(
            group_match.group('group_name'))

        group_counters = {}

        for counter_match in _PRE_YARN_COUNTER_RE.finditer(
                group_match.group('counter_list_str')):

            counter_name = _pre_yarn_history_unescape(
                counter_match.group('counter_name'))
            amount = int(counter_match.group('amount'))

            group_counters[counter_name] = amount

        counters[group_name] = group_counters

    return counters
