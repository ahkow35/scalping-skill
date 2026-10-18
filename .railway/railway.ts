import { bucket, defineRailway, github, preserve, project, service, volume } from "railway/iac";

// Railway settings for both services, replacing railway.json /
// railway.recorder.json (Config as Code, deprecated; stops working
// 2026-12-01). A repo-root railway.json was also applied to every service
// built from this repo, which made the recorder build the watcher's image.
// Secrets stay in Railway: preserve() keeps each existing value unchanged.
// Restart policy is left at Railway's default (on failure, 10 retries);
// setting it explicitly stores nothing and leaves a permanent plan diff.
export default defineRailway(() => {
  const repo = github("ahkow35/scalping-skill", { checkSuites: false });

  const watcherVolume = volume("scalping-skill-volume", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "asia-southeast1-eqsg3a", sizeMB: 5000 });
  const recorderVolume = volume("recorder-volume", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "asia-southeast1-eqsg3a", sizeMB: 5000 });
  const hypeTape = bucket("hype-tape", { region: "sin" });

  // Flow recorder (RECORDER-RAILWAY.md). One replica: the uploader's
  // HEAD-then-PUT check assumes a single writer.
  const recorder = service("recorder", {
    source: repo,
    build: { builder: "DOCKERFILE", dockerfilePath: "Dockerfile.recorder" },
    start: "python3 railway_record.py",
    healthcheck: "/health",
    healthcheckTimeout: 30,
    replicas: { "asia-southeast1-eqsg3a": 1 },
    volumeMounts: { "/data": recorderVolume },
    env: { PORT: preserve(), RECORDER_COINS: "HYPE,ZEC,PUMP", RECORDER_DISK_FLOOR_GB: "2", S3_ACCESS_KEY_ID: preserve(), S3_BUCKET: preserve(), S3_ENDPOINT: preserve(), S3_REGION: preserve(), S3_SECRET_ACCESS_KEY: preserve() },
  });

  // Account watcher (ACCOUNT-MONITOR.md).
  const watcher = service("scalping-skill", {
    source: repo,
    build: { builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    start: "python3 railway_watch.py",
    healthcheck: "/health",
    healthcheckTimeout: 30,
    replicas: { "asia-southeast1-eqsg3a": 1 },
    volumeMounts: { "/data": watcherVolume },
    env: {
      DATA_DIR: preserve(), FRED_API_KEY: preserve(), HEALTHCHECK_PING_URL: preserve(), MONITOR_DAILY_LOSS_USDC: preserve(), MONITOR_MAX_POSITION_NOTIONAL_USDC: preserve(), MONITOR_TIMEZONE: preserve(), MONITOR_WALLET: preserve(), REPORT_TOKEN: preserve(), TELEGRAM_BOT_TOKEN: preserve(), TELEGRAM_CHAT_ID: preserve(),
      RECORDER_STATUS_URL: "http://${{recorder.RAILWAY_PRIVATE_DOMAIN}}:8080/status",
      // Liquidity briefing (market_brief.py): plain defaults, not preserve() —
      // none of these exist on Railway yet. Same values as the code defaults.
      BRIEF_ENABLED: "true", BRIEF_TIMES: "08:00,20:30", BRIEF_COINS: "BTC,ETH,HYPE,ZEC",
      BRIEF_FUNDING_ALERT_PCT: "0.005", BRIEF_OI_DROP_PCT: "5", BRIEF_EVENT_MINUTES: "60",
    },
  });

  return project("ample-friendship", {
    resources: [recorder, watcher, watcherVolume, recorderVolume, hypeTape],
  });
});
