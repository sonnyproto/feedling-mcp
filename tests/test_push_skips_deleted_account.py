from unittest.mock import patch, MagicMock
from push import service as push_service


def _store_with_token(uid="usr_gone_0001"):
    s = MagicMock()
    s.user_id = uid
    return s


def test_deliver_skips_when_account_gone():
    store = _store_with_token()
    with patch("push.service.registry._user_entry_snapshot", return_value=None), \
         patch("push.service.apns._send_apns_to_active_tokens") as send:
        fields = push_service._deliver_ai_message_push_if_background(store, body="hi", title="")
        send.assert_not_called()
        assert fields.get("push_decision") == "skip"
        assert fields.get("push_reason") == "account_gone"


def test_send_chat_alert_skips_when_account_gone():
    store = _store_with_token()
    with patch("push.service.registry._user_entry_snapshot", return_value=None), \
         patch("push.service.apns._send_apns_to_active_tokens") as send:
        res = push_service._send_chat_alert(store, "hi")
        send.assert_not_called()
        assert res.get("reason") == "account_gone"


# --- 竞态：本 worker 内存注册表还没处理 users 重载(快照仍在)，但 DB 已删。
#     权威 DB 检查必须拦住这次推送。 ---

def test_deliver_skips_when_db_says_gone_despite_stale_registry():
    store = _store_with_token()
    with patch("push.service.registry._user_entry_snapshot", return_value={"user_id": store.user_id}), \
         patch("push.service.db.user_exists", return_value=False) as exists, \
         patch("push.service.apns._send_apns_to_active_tokens") as send:
        fields = push_service._deliver_ai_message_push_if_background(store, body="hi", title="")
        exists.assert_called_once_with(store.user_id)
        send.assert_not_called()
        assert fields.get("push_reason") == "account_gone"


def test_send_chat_alert_skips_when_db_says_gone_despite_stale_registry():
    store = _store_with_token()
    with patch("push.service.registry._user_entry_snapshot", return_value={"user_id": store.user_id}), \
         patch("push.service.db.user_exists", return_value=False), \
         patch("push.service.apns._send_apns_to_active_tokens") as send:
        res = push_service._send_chat_alert(store, "hi")
        send.assert_not_called()
        assert res.get("reason") == "account_gone"


def test_send_chat_alert_proceeds_when_account_exists():
    """正向对照：账号在内存又在 DB → gate 放行，真发 APNs（不是被误判成 account_gone）。"""
    store = _store_with_token()
    with patch("push.service.registry._user_entry_snapshot", return_value={"user_id": store.user_id}), \
         patch("push.service.db.user_exists", return_value=True), \
         patch("push.service.push_tokens._select_token", return_value={"token": "abc"}), \
         patch("push.service.apns._send_apns_to_active_tokens", return_value={"status": "ok"}) as send:
        res = push_service._send_chat_alert(store, "hi")
        send.assert_called_once()
        assert res.get("reason") != "account_gone"
