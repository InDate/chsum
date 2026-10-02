# `--debug`

Appended to any command, it prints beneath the output what the run read
(transcripts, record counts), ran (subprocesses, exit codes) and resolved (each
step, including fallbacks a normal run is silent about). It names records
without copying their text, so it reads only on the same machine.

Its `reproduce` lines rerun the command and open the conversation with
`claude-history agent read`.
