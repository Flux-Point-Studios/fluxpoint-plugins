import time
from worker import config, jobs, logs

CONFIG = None


def run(path):
    global CONFIG
    CONFIG = config.load(path)
    logs.setup()
    db = jobs.connect(CONFIG["db"]["url"])
    while True:
        jobs.run_batch(db, CONFIG["batch_size"])
        time.sleep(CONFIG["poll_seconds"])
