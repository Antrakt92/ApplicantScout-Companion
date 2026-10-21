import json

from applicant_scout import usage


def test_undecodable_usage_state_is_quarantined_and_stays_opted_out_on_restart(tmp_path):
    path = tmp_path / usage.USAGE_STATE_FILENAME
    corrupt = b'{"schema":1,"consent":true,"install_id":"\xff"}'
    path.write_bytes(corrupt)
    (tmp_path / usage.USAGE_INSTALLER_CHOICE_FILENAME).write_bytes(b'opt-in\n')
    sent = []

    def sender(_endpoint, payload):
        sent.append(payload)
        return 204

    first = usage.UsageClient(tmp_path, '0.24.2', _sender=sender)
    try:
        assert not first.consent_enabled
        assert not first.record('addon_received')
        assert json.loads(path.read_text(encoding='utf-8')) == {'schema': 1, 'consent': False}
        backups = list(tmp_path.glob('usage.json.corrupt-*'))
        assert len(backups) == 1
        assert backups[0].read_bytes() == corrupt
    finally:
        first.close()
    restarted = usage.UsageClient(tmp_path, '0.24.2', _sender=sender)
    try:
        assert not restarted.consent_enabled
        assert sent == []
        assert list(tmp_path.glob('usage.json.corrupt-*')) == backups
    finally:
        restarted.close()
