# bmtc-gps-healthcheck

Tool to track the health of GPS location tracking for selected BMTC bus routes in Bengaluru. The collector polls the Namma BMTC live tracking API, logs the GPS status, and captures screenshots when a route or bus is not trackable. The static dashboard shows recent tracking uptime, and replays historical bus positions.

Deployment of the dashboard for monitoring MF-22 metro feeder buses can be found at [https://bmtc-mf22-tracker.pages.dev/](https://bmtc-mf22-tracker.pages.dev/).

## Develop

- Create a virtual environment and run `pip install pandas pyarrow requests python-dotenv boto3 playwright pillow`
- Install Chromium for screenshots with `playwright install chromium`
- Configure `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, and `R2_BUCKET` in `.env`; `R2_PREFIX` is optional
- Start the data pipeline in a polling loop with `python main.py`
- Visualize the data on the dashboard locally with `python -m http.server` and open the dashboard page on a browser using http://localhost:8000/index.html

Use `python main.py --help` to see the list of configuration options for routes, polling frequency, and screenshots.

## Architecture

- [main.py](main.py) queries the Namma BMTC API for GPS location tracking and normalizes observations into `data/gps.parquet`, which gets uploaded to a Cloudflare R2 Bucket.
- Screenshots are captured with Playwright from the Namma BMTC web application route tracking page.
- [index.html](index.html) reads the `gps.parquet` on R2 directly with [hyparquet](https://github.com/hyparam/hyparquet), and visualizes the historical uptime and historical vehicle locations on an interactive map.

## Methodology

Each vehicle is classified as `OK` when its last GPS refresh is no more than 15 minutes old and `STALE` otherwise. A route is tracking when at least one vehicle is `OK`; a response with no vehicles is recorded as `NO_GPS`. Failed API requests appear as missing checks when other routes were recorded at the same poll time. Screenshots are optionally recorded and uploaded to R2 for GPS refreshes that are not `OK`.

## License

The code is licensed under [MIT](LICENSE).

## AI Declaration

Components of this repository, including code and documentation, were written
with assistance from AI models.
