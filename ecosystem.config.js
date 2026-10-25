module.exports = {
  apps: [{
    name: "uorm-mine",
    script: "uorm_mine.py",
    interpreter: "python3",
    cwd: "uorm-mine",
    autorestart: true,
    max_restarts: 50,
    restart_delay: 10000,
    max_memory_restart: "300M",
    out_file: "uorm-mine/pm2-out.log",
    error_file: "uorm-mine/pm2-error.log",
    env: {
      PYTHONUNBUFFERED: "1",
    },
  }],
};
