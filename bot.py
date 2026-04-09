"""
bot.py — Web push notification sender.

Sends push notifications to subscribed browsers when price drops are detected.
"""

import json
import logging

from pywebpush import webpush, WebPushException

import config
import db

log = logging.getLogger(__name__)


def send_push_notification(subscription_info: dict, title: str, body: str, url: str | None = None) -> bool:
    """
    Send a web push notification to a single subscription.

    Parameters
    ----------
    subscription_info : the browser's PushSubscription object (endpoint + keys)
    title             : notification title
    body              : notification body text
    url               : optional URL to open on click

    Returns True if sent successfully, False otherwise.
    """
    if not config.VAPID_PRIVATE_KEY:
        log.error("VAPID_PRIVATE_KEY not set — cannot send push notification")
        return False

    payload = {
        "title": title,
        "body": body,
    }
    if url:
        payload["url"] = url

    try:
        webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload),
            vapid_private_key=config.VAPID_PRIVATE_KEY,
            vapid_claims={
                "sub": f"mailto:{config.VAPID_CLAIMS_EMAIL}",
            },
        )
        log.info("Push notification sent: %s", title)
        return True
    except WebPushException as e:
        log.error("Push notification failed: %s", e)
        # If the subscription is expired/invalid, clean it up
        if e.response and e.response.status_code in (404, 410):
            log.warning("Subscription expired — removing from database")
            try:
                endpoint = subscription_info.get("endpoint", "")
                db.remove_subscription_by_endpoint(endpoint)
            except Exception:
                pass
        return False
    except Exception as e:
        log.error("Unexpected push error: %s", e)
        return False


def notify_watch_subscribers(watch_id: int, title: str, body: str, url: str | None = None) -> int:
    """
    Send a push notification to all subscribers of a given watch.

    Returns the number of successful sends.
    """
    subscriptions = db.get_subscriptions_for_watch(watch_id)
    if not subscriptions:
        log.info("No subscribers for watch #%d", watch_id)
        return 0

    sent = 0
    for sub in subscriptions:
        try:
            sub_info = json.loads(sub["subscription_json"])
        except (json.JSONDecodeError, KeyError):
            log.warning("Invalid subscription JSON for id #%d", sub["id"])
            continue

        if send_push_notification(sub_info, title, body, url):
            sent += 1

    log.info("Sent %d/%d push notifications for watch #%d", sent, len(subscriptions), watch_id)
    return sent
