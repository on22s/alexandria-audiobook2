# Thunder operations

Rules for running jobs on the remote Thunder GPU machines.

## Before you change anything

- Each machine runs **one GPU job at a time**.
- Check the process list and the job's log first.

Useful checks:

```bash
date '+%I:%M %p %Z'
ps -eo pid,ppid,etime,cmd
tail -f /home/ubuntu/<job>/eval.log
```

## Changing a running job

- **Never edit a shell script while it is running.** Bash reads scripts as it goes, so an
  edit can make the running job jump to the wrong place. Write a new version under a new name
  and launch that instead.
- **Stop jobs by PID**, and only when a run is known to be invalid.
- **Never use `pkill -f`** with a job pattern — it can match, and kill, the shell running it.

## Reporting progress

For a job that is still running, report the measured time per window, and keep it separate
from the estimate of how much work is left.
