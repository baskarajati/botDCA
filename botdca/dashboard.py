from pathlib import Path

DASHBOARD_HTML = (Path(__file__).with_name("static") / "console.html").read_text()
