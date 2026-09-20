# Thunder operations

Thunder jobs use one GPU at a time. Check the process list and the job log
before changing anything. Do not edit a running shell script; create a new
version and launch that instead.

Useful checks:

```bash
date '+%I:%M %p %Z'
ps -eo pid,ppid,etime,cmd
tail -f /home/ubuntu/<job>/eval.log
```

Kill by the specific PID only when a run is known-invalid. Never use
`pkill -f` against a job pattern.

For an in-flight job, report measured per-window timing and distinguish it
from an estimate for the remaining work.
