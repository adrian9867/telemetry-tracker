import time

from src import db, providers

POLL_SECONDS = 1
HEARTBEAT_SECONDS = 60


def run() -> None:
    con = db.connect()
    db.init(con)

    current_app = None
    last_heartbeat = 0.0

    print("tracking... press Ctrl+C to stop")
    try:
        while True:
            now = time.time()
            app = providers.foreground_process() or "__UNKNOWN__"

            if app != current_app:
                with con:
                    con.execute(
                        "INSERT INTO switches(ts, app) VALUES (?, ?)", (now, app)
                    )
                current_app = app

            if now - last_heartbeat >= HEARTBEAT_SECONDS:
                with con:
                    con.execute("INSERT INTO heartbeat(ts) VALUES (?)", (now,))
                last_heartbeat = now

            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("stopped")
    finally:
        con.close()


if __name__ == "__main__":
    run()