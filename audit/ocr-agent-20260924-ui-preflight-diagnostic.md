# Dual-UI mixed stability preflight diagnosis (2026-09-24)

The original preflight at `F:\OCR Agent 测试\mixed-candidate06-preflight-01` failed its fixed 30-second Playwright assertion for `MIXED-DONE UI-2-0`. Its business and official checkpoint databases were inspected read-only after shutdown; both UI runs reached `completed/answered`. UI-1-0 ran from 2026-09-23T20:50:30.234Z to 20:51:01.965Z (31.73 s); UI-2-0 ran from 20:50:34.259Z to 20:51:04.618Z (30.36 s). Their persisted assistant messages and completion events are present. Neither run waited for authorization or a user decision. The UI failure is a real latency failure under the existing test limit, rather than lost business work.

The original HTTP span trace shows a mixed-phase message POST at 11.014 s: 4.983 s before server entry, 4.179 s inside the server, and 1.852 s after server exit. Other simultaneous `/state` and visual requests took 5–10 s. The business runs spent about 11–12 s queued. These tails cannot be explained as a page locator issue. The original trace did not include database internals.

Two new isolated short runs used the same candidate-06 asset manifest, frozen source SHA-256 `67d5382671b22c0cd366a16c0f42fc622a14b565e1aa539ed2bde4f82e92cb6a`, one measured page, one repeat, 10-second soak, per-request transport, paired transport probe, and API/database tracing. No product source or timeouts were changed. F: is `ST1000DM003-1ER162` SATA HDD; E: is `Acer SSD N7000 2TB` NVMe. The outputs are:

| Measurement | F: HDD diagnostic (`F:\OCR Agent 测试\mixed-candidate06-ui-diagnostic-01`) | E: NVMe diagnostic (`E:\OCR Agent 测试\mixed-candidate06-ui-diagnostic-ssd-01`) |
| --- | ---: | ---: |
| Raw short-run status | failed, same UI-2-0 30 s assertion | pass, each UI client 3 cycles |
| Agent OCR batch wall time | 39.421 s | 20.978 s |
| Mixed two-UI OCR batch wall time | 56.293 s | 26.715 s |
| Mixed message POST client p95 | 9,198 ms, 6 samples | **1,344 ms, 10 samples** |
| Mixed message POST server p95 | 6,531 ms, 8 samples | 975 ms, 14 samples |
| Mixed `/state` client p95 | 4,349 ms, 53 samples | **1,029 ms, 42 samples** |
| Mixed run progress GET client p95 | 1,588 ms, 79 samples | 728 ms, 68 samples |
| Mixed business Store lock wait maximum | 586 ms | 24 ms |
| Mixed UI completion p95 | no receipt; UI exited | 9,019 ms, 3 receipts |
| Mixed visible progress p95 | no receipt | 1,587 ms, 3 receipts |

On E:, the first UI-2-0 and UI-1-0 business runs completed in 6.91 s and 7.36 s. The 1,344 ms message POST consists of 826 ms before server entry, 505 ms in the server, and 12 ms after server exit; the next slowest 1,311 ms consists of 631 + 505 + 174 ms. Residual latency spans both request scheduling and server execution, not just SQLite business lock waits. The synthetic audit's saver `aput`/`aput_writes` histogram is absent in both runs, so the trace does not isolate official checkpoint write time or prove a single underlying I/O mechanism. The medium and the run result are strongly associated, but cold cache, antivirus, and concurrent host activity were not controlled as independent variables.

`assess_mixed_stability.py` reports functional pass for the E: short run, **not performance qualification**. Under its unchanged 1,000 ms API p95 and 2,000 ms visible-progress p95 limits, E: still exceeds: `agent_off:api` 1,009 ms (7), `mixed_two_ui:state_api` 1,029 ms (26), `mixed_two_ui:api` 1,344 ms (18), `soak:api` 1,313 ms (18), and `soak:ui_progress` 2,183 ms (3). The per-route mixed message POST 1,344 ms (10) and `/state` 1,029 ms (42) show the contributors. The evaluator aggregates `api` by phase and `state_api` separately; it does not automatically gate the message route in isolation. This is one repeat and 10 seconds of soak, far short of the required three repeats and four hours. Do not relax the UI timeout or acceptance thresholds based on this result; investigate the remaining >1 s API tails and repeat on the intended target medium before qualification.

The three raw output directories and both databases are retained unchanged. The new short outputs occupy approximately 27.1 MB on F: and 30.4 MB on E:. No old receipt, P0 source, runtime, checkpoint implementation, or sealed input was edited.
