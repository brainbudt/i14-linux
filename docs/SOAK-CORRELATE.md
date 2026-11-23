# Correlate DNS failures with sampled power

Use the offline correlator to find which DNS checks failed and whether the phone reported external power around each unplanned failure. Run it on the host with existing log files and Python 3.10 or newer.

```sh
python3 -B scripts/i14-soak-correlate.py \
  --dns dns.csv.1 --dns dns.csv \
  --power samples.jsonl.1 --power samples.jsonl \
  --maintenance maintenance.txt \
  --dns-max-gap-seconds 360 --power-max-gap-seconds 180
```

Supply rotations oldest first. Choose gap limits for the recording cadence and the delay you consider acceptable. Both limits are required. You can omit power and maintenance inputs.

## What to read in the report

The command writes one JSON object with schema `i14-soak-correlate/1`. Start with `coverage.status` and `coverage.gaps`. Coverage spans the first through the last DNS observation. A single observation gives incomplete coverage. Between successive observations, the tool subtracts planned maintenance and reports each remaining gap strictly longer than the DNS limit.

`dns` counts passed, planned-failure and unplanned-failure observations. A probe with several failed checks counts once. `unplanned_failures` lists each such observation's UTC time, failed check names, and `external_power`. The totals by power state appear in `unplanned_by_power_state`.

| Exit | Meaning | Output |
| --- | --- | --- |
| 0 | DNS coverage complete and all failures, if any, were planned | JSON on stdout |
| 1 | DNS coverage complete with at least one unplanned failure | JSON on stdout |
| 2 | DNS coverage incomplete | JSON on stdout, including observed failures |
| 2 | Invalid arguments or input | Diagnostic on stderr, empty stdout |

## Input files

- **DNS CSV** comes from `scripts/i14-dns-soak.py`. Each file needs its recorder header and one physical line per probe. The five expected results in order are `NOERROR`, `NOERROR`, `NOERROR`, `NOERROR+AD`, and `SERVFAIL`. A different nonempty answer counts as a failure. Each latency must be an unsigned decimal integer between zero and 2⁶³−1 milliseconds.
- **Power JSONL** uses the top-level fields in the soak sampler's `samples.jsonl`. Each line needs a numeric POSIX `epoch`, a nonempty string `boot_id`, and numeric nonnegative `uptime_seconds`. The numeric `usb_online` and `ac_online` fields can each be 0, 1, null or absent. Other fields, including the sampler's own DNS check, are ignored. Duplicate JSON keys and nonstandard JSON constants invalidate a sample.
- **Maintenance** takes one `START END [reason]` per line. Blank lines and lines starting with `#` are allowed. Both endpoints are inclusive. Touching or overlapping windows merge. A point window splits a gap at that instant and exempts only a failure recorded at that instant.

All inputs use UTF-8. Each DNS or power file can contain up to 16 MiB, each maintenance file up to 1 MiB. At least one DNS observation is required. DNS and maintenance timestamps use a date and time with seconds, optionally a fraction through nanoseconds and a UTC offset. A missing offset means UTC. Use `T` between date and time in the whitespace-delimited maintenance file.

Each input stream must be chronological across rotations. Adjacent identical observations at one instant collapse. A different DNS answer or latency at the same instant, conflicting power fields, or a timestamp moving backwards produces an error. Power comparisons use the five fields above and treat absent and null online flags alike. Time comparisons use integer nanoseconds.

## Interpreting power

A failure at time `t` uses the left sample of the adjacent interval `[a, b)` containing `t`. Both samples must share a boot ID. Elapsed wall time and uptime must each increase by at most the selected power gap and agree within two seconds. Either online flag equal to 1 yields `online`. Two zeros yield `offline`.

An `unknown` state includes a `power_reason` naming the missing evidence, such as a reboot, clock disagreement, or absent online flags. The last power sample cannot establish an interval on its own. The clocks of both recorders must already be aligned. Power changes between samples can go unseen, and a matching time alone cannot establish why DNS failed.

The JSON contains file counts, timestamps and check names. File errors include the supplied path and, when available, the line number.
