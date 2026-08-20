window.ROBOT_APP_CONFIG = window.ROBOT_APP_CONFIG || {
  // Web pages use their current server origin automatically.  The packaged
  // Android App has no remote origin, so it tries the relay's Tailscale and
  // campus addresses in order.  No phone or robot address belongs here.
  serverBase: "auto",
  serverBases: [
    "http://100.125.188.94:8765",
    "http://10.249.188.197:8765"
  ],
  token: "REDACTED_CONFIGURE_LOCALLY"
};
