"""Repeated editor saves need the hash of normalized content, not the submitted text."""
import hashlib


def test_markdown_save_returns_a_version_for_the_next_save(api):
    client = api.as_user('alice')
    created = client.post('/api/skills/add', json={'name': 'editor-guide', 'description': 'Writing guide', 'procedure': ['Write clearly']})
    assert created.status_code == 200, created.text
    original = client.get('/api/skills/editor-guide/markdown').json()
    first = client.post('/api/skills/editor-guide/markdown', json={'markdown': original['markdown'] + '\nFirst edit\n', 'version': original['version']})
    assert first.status_code == 200, first.text
    saved = first.json()
    assert saved['version'] == hashlib.sha256(saved['markdown'].encode()).hexdigest()
    assert client.get('/api/skills/editor-guide/markdown').json()['markdown'] == saved['markdown']
    second = client.post('/api/skills/editor-guide/markdown', json={'markdown': saved['markdown'] + '\nSecond edit\n', 'version': saved['version']})
    assert second.status_code == 200, second.text
