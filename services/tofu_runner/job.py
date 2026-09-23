"""One Kubernetes Job, one draft snapshot, one structured result on stdout."""
import gzip
import json
import sys

from server import app

if __name__ == '__main__':
    try:
        with gzip.open('/input/payload.json.gz', 'rb') as source:
            data = source.read(3_000_001)
        if len(data) > 3_000_000:
            raise ValueError('Draft snapshot exceeds the runner limit.')
        payload = json.loads(data)
        response = app.test_client().post('/run', json=payload)
        result = response.get_json()
        if not isinstance(result, dict):
            raise ValueError('The runner could not produce a result.')
    except Exception as exc:
        result = {'passed': False, 'error': f'Runner failed: {type(exc).__name__}: {exc}'}
    print('PRAXIS_RESULT=' + json.dumps(result), flush=True)
    sys.exit(0 if result.get('passed') else 1)
