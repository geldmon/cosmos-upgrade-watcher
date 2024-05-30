import time
import requests
import logging
import sqlite3
import slack_sdk
from prometheus_client import Gauge, Counter


class UpgradeRequestFailed(Exception):
    pass


class PlanNotInRequest(Exception):
    pass

class BlockNotInRequest(Exception):
    pass

class BlockRequestFailed(Exception):
    pass


def fetch_upgrade_plan(endpoint: str):
    req = requests.get(f"{endpoint}/cosmos/upgrade/v1beta1/current_plan")

    if req.status_code != 200:
        raise UpgradeRequestFailed(req.text)

    if "plan" not in req.json():
        raise PlanNotInRequest()

    return req.json()["plan"]

def fetch_block_height(endpoint: str):
    req = requests.get(f"{endpoint}/status")

    if req.status_code != 200:
        raise BlockRequestFailed(req.text)

    if "result" not in req.json():
        raise BlockNotInRequest()

    return int(req.json()['result']['sync_info']['latest_block_height'])

class ChainWatcher:
    def __init__(
        self,
        config,
        chain_id,
    ):
        self.chain_id = chain_id
        self.config = config
        self.webhook_client = slack_sdk.WebhookClient(config["slack"]["webhook"])
        self.logger = logging.getLogger(self.chain_id)

    def monitor(self):
        self.db_conn = sqlite3.connect(self.config["db_path"])
        self.last_checked_time = Gauge(f'cosmos_upgrade_watcher_last_checked', 'Last time an upgrade was fetched', ["chain_id"])
        self.error_counter = Counter(f'cosmos_upgrade_watcher_errors', 'Errors encountered during an upgrade', ["chain_id", "error"])

        logging.basicConfig(
            level=logging.DEBUG, format="%(asctime)s %(name)s %(levelname)s:%(message)s"
        )
        logging.getLogger("requests").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("slack_sdk.webhook.client").setLevel(logging.WARNING)

        self.logger.debug(f"starting watcher loop")

        while True:
            time.sleep(self.config["interval_seconds"])
            try:
                self.logger.debug(f"fetching upgrade plans")
                plan = fetch_upgrade_plan(
                    self.config["chains"][self.chain_id]["endpoint"]
                )
            except PlanNotInRequest:
                self.logger.error("request successful, but no plan object in response")
                self.error_counter.labels(chain_id=self.chain_id, error="plan_not_in_request").inc()
                return
            except UpgradeRequestFailed as e:
                self.logger.error("upgrade plan request failed", e.args)
                self.error_counter.labels(chain_id=self.chain_id, error="upgrade_request_failed").inc()
                return

            self.last_checked_time.labels(chain_id=self.chain_id).set(time.time())

            if not plan:
                self.logger.debug(f"no current upgrade plan")
                continue

            existing_upgrade = self.get_db_upgrade()
            if existing_upgrade and existing_upgrade[1] >= int(plan["height"]):
                try:
                    current_block = fetch_block_height(
                        self.config["chains"][self.chain_id]["rpc"]
                    )
                except BlockNotInRequest:
                    self.logger.error("request successful, but no block object in response")
                    self.error_counter.labels(chain_id=self.chain_id, error="block_not_in_request").inc()
                    return
                except BlockRequestFailed as e:
                    self.logger.error("block request failed", e.args)
                    self.error_counter.labels(chain_id=self.chain_id, error="block_request_failed").inc()
                    return

                if not existing_upgrade[2] and existing_upgrade[1] - current_block <= self.config["chains"][self.chain_id]["reminder_diff_blocks"]:
                    self.logger.debug(f"upgrade time in less than {self.config['chains'][self.chain_id]['reminder_diff_blocks']} blocks")
                    self.remind_slack(plan, current_block)
                    self.update_db_reminder()

                self.logger.debug(f"no new upgrade plan")
                continue

            self.notify_slack(plan)
            self.update_db_upgrade(plan)


    def get_db_upgrade(self):
        cursor = self.db_conn.cursor()
        res = cursor.execute(f"SELECT * FROM chains WHERE chain_id='{self.chain_id}'")
        return res.fetchone()

    def update_db_upgrade(self, plan):
        cursor = self.db_conn.cursor()
        try:
            cursor.execute(
                f"INSERT OR REPLACE into chains(chain_id, upgrade_height, is_reminder_sent) VALUES('{self.chain_id}', {int(plan['height'])}, {False})"
            )
            self.db_conn.commit()
        except sqlite3.OperationalError as e:
            self.logger.error("failed to update database with new upgrade")
            self.error_counter.labels(chain_id=self.chain_id, error="database_update_failed").inc()

    def update_db_reminder(self):
        cursor = self.db_conn.cursor()
        try:
            cursor.execute(
                f"UPDATE chains SET is_reminder_sent={True} WHERE chain_id='{self.chain_id}'"
            )
            self.db_conn.commit()
        except sqlite3.OperationalError as e:
            self.logger.error(f"failed to update database with new reminder, {e}")
            self.error_counter.labels(chain_id=self.chain_id, error="database_update_failed").inc()

    def notify_slack(self, plan):
        self.webhook_client.send(
            text=f"{self.chain_id} upgrade at block height {plan['height']}",
            blocks=[
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"New chain upgrade in {self.chain_id}",
                        "emoji": False,
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"Name\n`{plan['name']}`"},
                        {"type": "mrkdwn", "text": f"Height\n`{plan['height']}`"},
                    ],
                },
            ],
        )

    def remind_slack(self, plan, block):
        self.webhook_client.send(
            text=f"{self.chain_id} chain upgrade taking place in {int(plan['height']) - block} blocks! @here",
            blocks=[
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"Reminder of chain upgrade in {self.chain_id}",
                        "emoji": False,
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"Name\n`{plan['name']}`"},
                        {"type": "mrkdwn", "text": f"Height\n`{plan['height']}`"},
                        {"type": "mrkdwn", "text": f"Current Height\n`{block}`"},
                    ],
                },
            ],
        )


