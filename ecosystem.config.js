const path = require("node:path");

module.exports = {
  apps: [{
    name: "astral_bot",
    cwd: __dirname,
    script: "main.py",
    interpreter: path.join(__dirname, "venv", "Scripts", "python.exe"),
    args: "--admin-host 0.0.0.0 --admin-port 5001",
    autorestart: true,
    restart_delay: 5000
  }]
}
