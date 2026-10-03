"""Disposable OpenTofu workspaces with init-only private Git authentication."""
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import tempfile
import threading
import time

from flask import Flask, jsonify, request
import hcl2

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 3_000_000
BUSY = threading.Lock()
NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_-]*$')


def literal(value):
    return json.dumps(value, allow_nan=False).replace('${', '$${').replace('%{', '%%{')


def prepare(files, root, include_tests=False):
    if not isinstance(files, dict) or not files or len(files) > 200:
        raise ValueError('Provide 1–200 draft files.')
    if sum(len(v.encode()) for v in files.values()) > 2_000_000:
        raise ValueError('Draft exceeds 2 MB.')
    for name, content in files.items():
        path = PurePosixPath(name)
        if path.is_absolute() or any(p in ('.', '..', '.terraform', '.git', '.praxis-tests') for p in path.parts) or '\\' in name or '\x00' in name or not path.parts:
            raise ValueError('Unsafe draft file path.')
        # Existing test files never execute; Praxis creates its own plan-only test.
        if name.endswith(('.tfstate', '.tfstate.backup')) or (not include_tests and name.endswith(('.tftest.hcl', '.tftest.json', '.tofutest.hcl', '.tofutest.json'))):
            continue
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


def configs(root):
    result = []
    for pattern in ('*.tf', '*.tf.json', '*.tofu', '*.tofu.json'):
        for path in root.glob(pattern):
            result.append(json.loads(path.read_text()) if path.suffix == '.json' else hcl2.loads(path.read_text()))
    return result


def blocks(value):
    return [value] if isinstance(value, dict) else value


def mock_test(root, settings):
    providers = {}
    outputs = set()
    for doc in configs(root):
        for entry in blocks(doc.get('output', [])):
            outputs.update(entry)
        for terraform in blocks(doc.get('terraform', [])):
            for required in blocks(terraform.get('required_providers', [])):
                for name, spec in required.items():
                    aliases = spec.get('configuration_aliases', []) if isinstance(spec, dict) else []
                    providers.setdefault(name, set()).update(str(a).removeprefix('${').removesuffix('}') for a in aliases)
        for block in blocks(doc.get('provider', [])):
            for name, spec in block.items():
                providers.setdefault(name, set())
                if spec.get('alias'):
                    providers[name].add(name + '.' + spec['alias'])
        for kind in ('resource', 'data'):
            for block in blocks(doc.get(kind, [])):
                for resource_type in block:
                    name = resource_type.split('_', 1)[0]
                    if name != 'terraform':
                        providers.setdefault(name, set())
    defaults = settings.get('data_defaults', {})
    for key, value in defaults.items():
        if not NAME.fullmatch(key) or not isinstance(value, dict):
            raise ValueError('Mock data defaults must map data source types to JSON objects.')
        if key.split('_', 1)[0] not in providers:
            raise ValueError(f'No root provider matches mock data type {key}.')
    lines = []
    for name, aliases in sorted(providers.items()):
        if not NAME.fullmatch(name):
            raise ValueError('Unsupported provider name in mock test.')
        for address in [name] + sorted(aliases):
            if address != name and not re.fullmatch(re.escape(name)+r'\.[A-Za-z_][\w-]*', address):
                raise ValueError('Unsupported provider alias in mock test.')
            lines.append(f'mock_provider "{name}" {{')
            if address != name:
                lines.append('  alias = ' + literal(address.split('.', 1)[1]))
            for resource_type, values in defaults.items():
                if resource_type.startswith(name + '_'):
                    lines.append(f'  mock_data "{resource_type}" {{\n    defaults = {literal(values)}\n  }}')
            lines.append('}')
    lines.append('run "praxis_mock_plan" {\n  command = plan')
    variables = settings.get('variables', {})
    if variables:
        lines.append('  variables {')
        for name, value in variables.items():
            if not NAME.fullmatch(name):
                raise ValueError('Invalid test variable name.')
            lines.append(f'    {name} = {literal(value)}')
        lines.append('  }')
    for name, expected in settings.get('expected_outputs', {}).items():
        if name not in outputs or not NAME.fullmatch(name):
            raise ValueError(f'Expected output {name} is not declared in this draft.')
        lines.append(f'  assert {{\n    condition = output.{name} == {literal(expected)}\n    error_message = "Output {name} did not match its expected value."\n  }}')
    lines.append('}')
    return '\n'.join(lines) + '\n'


def command(root, args, environment, timeout, offline=False):
    cmd = ['/usr/local/bin/tofu'] + args
    if offline:
        cmd = ['/usr/local/bin/python', '/runner/offline.py'] + cmd
    start = time.monotonic()
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(cmd, cwd=root, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        expired = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            expired = True
        finally:
            # Also terminate provider or helper descendants after command completion.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        output.seek(0)
        text = output.read(100_001).decode('utf-8', errors='replace')
    if len(text) > 100_000:
        text = text[:100_000] + '\n[Output truncated]'
    if expired:
        text += '\nCommand exceeded its time limit.'
    return {'command':'tofu ' + ' '.join(args), 'passed':not expired and process.returncode == 0,
            'output':text, 'seconds':round(time.monotonic()-start, 1)}


def git_environment(environment, home, auth):
    """Create an init-only, github.com-scoped helper without embedding its token in URLs."""
    if auth is None:
        return environment, []
    if (not isinstance(auth, dict) or auth.get('host') != 'github.com'
            or not isinstance(auth.get('token'), str)
            or not auth['token'] or len(auth['token']) > 512
            or any(character.isspace() for character in auth['token'])):
        raise ValueError('Invalid Git authentication supplied to the runner.')
    token_file = home / '.praxis-github-token'
    helper = home / '.praxis-git-credential'
    token_file.write_text(auth['token'])
    token_file.chmod(0o600)
    helper.write_text('''#!/bin/sh
case "$1" in
  get)
    printf '%s\\n' 'username=x-access-token'
    printf '%s' 'password='
    cat "$PRAXIS_GIT_TOKEN_FILE"
    printf '\\n'
    ;;
esac
''')
    helper.chmod(0o700)
    result = dict(environment)
    result.update(GIT_CONFIG_COUNT='1',
                  GIT_CONFIG_KEY_0='credential.https://github.com.helper',
                  GIT_CONFIG_VALUE_0='!' + str(helper),
                  PRAXIS_GIT_TOKEN_FILE=str(token_file))
    return result, [helper, token_file]


@app.get('/healthz')
def health():
    return 'ok'


@app.post('/run')
def run():
    if not BUSY.acquire(blocking=False):
        return jsonify(error='The test runner is busy. Try again after the current run finishes.'), 409
    try:
        payload = request.get_json()
        mode = payload.get('mode')
        if mode not in ('validate', 'mock', 'format'):
            raise ValueError('Choose validation or mock testing.')
        settings = payload.get('settings', {})
        for key in ('variables', 'data_defaults', 'expected_outputs'):
            if not isinstance(settings.get(key, {}), dict):
                raise ValueError('Test settings must be JSON objects.')
        with tempfile.TemporaryDirectory(prefix='praxis-tofu-') as directory:
            root = Path(directory) / 'work'
            root.mkdir()
            prepare(payload['files'], root, include_tests=True)
            home = Path(directory) / 'home'
            home.mkdir()
            environment = {'PATH':'/usr/local/bin:/usr/bin:/bin', 'HOME':str(home), 'TMPDIR':directory,
                           'TF_IN_AUTOMATION':'1', 'TF_INPUT':'0', 'CHECKPOINT_DISABLE':'1',
                           'AWS_EC2_METADATA_DISABLED':'true', 'GIT_TERMINAL_PROMPT':'0',
                           'GIT_CONFIG_NOSYSTEM':'1', 'GIT_ALLOW_PROTOCOL':'https', 'TF_CLI_CONFIG_FILE':str(home/'tofurc')}
            (home/'tofurc').write_text('disable_checkpoint = true\n')
            if mode == 'format':
                step = command(root, ['fmt', '-recursive', '-no-color'], environment, 60, offline=True)
                changed = {name: (root / name).read_text() for name in payload['files']
                           if (root / name).is_file() and (root / name).read_text() != payload['files'][name]}
                return jsonify(passed=step['passed'], steps=[step], formatted_files=changed,
                               version='1.12.0', mode=mode)
            fmt = command(root, ['fmt', '-check', '-diff', '-recursive', '-no-color'], environment, 60, offline=True)
            if not fmt['passed']:
                fmt['output'] = fmt.get('output', '') + '\nUse Format files on the saved draft, then run checks again.'
                return jsonify(passed=False, steps=[fmt], version='1.12.0', mode=mode)
            for name in payload['files']:
                if name.endswith(('.tftest.hcl', '.tftest.json', '.tofutest.hcl', '.tofutest.json')):
                    (root / name).unlink(missing_ok=True)
            test_source = mock_test(root, settings) if mode == 'mock' else ''
            # No user-supplied test file or backend state is used.
            init_environment, credential_files = git_environment(
                environment, home, payload.get('git_auth'))
            try:
                init = command(root, ['init','-backend=false','-input=false','-no-color'],
                               init_environment, 180)
            finally:
                for credential_file in credential_files:
                    credential_file.unlink(missing_ok=True)
            steps = [fmt, init]
            if steps[-1]['passed']:
                steps.append(command(root, ['validate','-no-color'], environment, 60, offline=True))
            if mode == 'mock' and steps[-1]['passed']:
                test_dir = root / '.praxis-tests'
                test_dir.mkdir()
                (test_dir/'praxis.tftest.hcl').write_text(test_source)
                steps.append(command(root, ['test','-no-color','-test-directory=.praxis-tests','-filter=.praxis-tests/praxis.tftest.hcl'], environment, 120, offline=True))
            return jsonify(passed=all(step['passed'] for step in steps), steps=steps, test_source=test_source,
                           version='1.12.0', mode=mode, check_suite=2)
    except (ValueError, TypeError, KeyError) as exc:
        return jsonify(error=str(exc)), 400
    finally:
        BUSY.release()
