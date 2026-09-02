<!-- GENERATED FILE - DO NOT EDIT. -->
<!-- Source: contracts/r300-link.json -->
<!-- Regenerate: python3 scripts/r300/link_contract.py generate -->
<!-- r300-link-sha256:29f2f1859f8436d95040e46e33088260242a9141ccccad423b9ca28e59bb3bf2 -->

# R300 link contract

R300 AI Motor cross-profile link contract. Single source of truth for the STM32<->Jetson UART frame, the STM32<->ATmega128A supervised heartbeat, the safety state machine and the pin map. Every consumer file is generated; editing a consumer by hand is a defect even when the value still matches.

## Acquisition

| Item | Value |
| --- | --- |
| Sample rate | 1000 S/s |
| Channels | vib_x, vib_y, vib_z, current |
| Channel units | MPU-6050 +/-16 g signed count, MPU-6050 +/-16 g signed count, MPU-6050 +/-16 g signed count, mA after motor-off zero calibration |
| Block | 128 samples |
| FFT window / hop | 512 / 256 |
| UART | 921600 8N1 |
| AVR diagnostic UART | 38400 8N1 |

## TELEMETRY frame (firmware-to-jetson)

| Offset | Size | Field | Type | Meaning |
| --- | --- | --- | --- | --- |
| 0 | 2 B | `sof` | uint16 | frame sync |
| 2 | 2 B | `version_flags` | uint16 | protocol version in bits 7:0, sensor and fault flags in bits 15:8 |
| 4 | 4 B | `sequence` | uint32 | monotonic counter; a gap is a packet loss event |
| 8 | 4 B | `timestamp_us` | uint32 | STM32 microsecond counter at the first sample of the block |
| 12 | 2 B | `sample_count` | uint16 | samples per channel in this frame; nominal 128 |
| 14 | variable | `payload` | int16[] | sample_count groups of vib_x, vib_y, vib_z, current interleaved |
| end-2 | 2 B | `crc16` | uint16 | CRC over sof through payload inclusive |

## HEALTH frame (jetson-to-firmware)

| Offset | Size | Field | Type | Meaning |
| --- | --- | --- | --- | --- |
| 0 | 2 B | `sof` | uint16 | frame sync |
| 2 | 2 B | `version_flags` | uint16 | protocol version in bits 7:0, reserved in bits 15:8 |
| 4 | 4 B | `sequence` | uint32 | monotonic counter |
| 8 | 4 B | `timestamp_ms` | uint32 | Jetson monotonic milliseconds |
| 12 | 2 B | `health_flags` | uint16 | bit0 app_alive, bit1 model_loaded, bit2 inference_ok, bit3 storage_ok |
| 14 | 2 B | `crc16` | uint16 | CRC over sof through health_flags inclusive |

## MOTOR_COMMAND frame (jetson-to-firmware)

| Offset | Size | Field | Type | Meaning |
| --- | --- | --- | --- | --- |
| 0 | 2 B | `sof` | uint16 | command sync distinct from telemetry and health |
| 2 | 2 B | `version_flags` | uint16 | protocol version in bits 7:0, reserved in bits 15:8 |
| 4 | 4 B | `sequence` | uint32 | monotonic command counter |
| 8 | 4 B | `timestamp_ms` | uint32 | Jetson monotonic milliseconds |
| 12 | 2 B | `duty_permille` | uint16 | requested TIM4 duty from 0 through 1000 permille |
| 14 | 2 B | `direction` | uint16 | 0 STOP or 1 FORWARD; reverse is excluded by the v4 one-direction pin map |
| 16 | 2 B | `crc16` | uint16 | CRC over sof through direction inclusive |

## STM_DIAGNOSTIC frame (firmware-to-jetson)

| Offset | Size | Field | Type | Meaning |
| --- | --- | --- | --- | --- |
| 0 | 2 B | `sof` | uint16 | diagnostic sync distinct from telemetry |
| 2 | 2 B | `version_flags` | uint16 | protocol version in bits 7:0, reserved in bits 15:8 |
| 4 | 4 B | `sequence` | uint32 | monotonic diagnostic counter |
| 8 | 4 B | `timestamp_ms` | uint32 | STM32 monotonic milliseconds |
| 12 | 4 B | `telemetry_sequence` | uint32 | next telemetry sequence number |
| 16 | 4 B | `dropped_blocks` | uint32 | acquisition blocks dropped before transmission |
| 20 | 4 B | `invalid_health_frames` | uint32 | invalid Jetson HEALTH frames |
| 24 | 4 B | `invalid_health_crc_frames` | uint32 | HEALTH frames rejected specifically for CRC |
| 28 | 4 B | `sensor_overruns` | uint32 | vibration samples overwritten before block commit |
| 32 | 4 B | `platform_vibration_overruns` | uint32 | MPU-6050 data-ready events overwritten before I2C service |
| 36 | 4 B | `platform_adc_overruns` | uint32 | ADC DMA ring overwritten before main-loop ownership polling |
| 40 | 4 B | `sensor_bus_errors` | uint32 | I2C sensor-bus timeout or transfer failures |
| 44 | 4 B | `uart_errors` | uint32 | Jetson UART or TX DMA failures |
| 48 | 4 B | `avr_uart_errors` | uint32 | AVR diagnostic UART failures |
| 52 | 4 B | `adc_dma_errors` | uint32 | current ADC DMA error state |
| 56 | 4 B | `uart_dma_errors` | uint32 | Jetson UART RX or telemetry TX DMA error state |
| 60 | 4 B | `adc_peripheral_errors` | uint32 | ADC peripheral overrun state |
| 64 | 4 B | `uart_busy_events` | uint32 | transmit requests delayed by an active DMA frame |
| 68 | 4 B | `invalid_avr_diag_frames` | uint32 | invalid AVR diagnostic frames |
| 72 | 4 B | `invalid_motor_command_frames` | uint32 | invalid Jetson MOTOR_COMMAND frames |
| 76 | 2 B | `current_zero_count` | uint16 | motor-off ADC zero calibration used for the current mA conversion |
| 78 | 2 B | `buffered_samples` | uint16 | samples currently retained in the acquisition block |
| 80 | 2 B | `status_flags` | uint16 | STM32 supervision bits 0:3 and fresh bridged AVR live-status bits 4:9 |
| 82 | 2 B | `crc16` | uint16 | CRC over all diagnostic fields |

## AVR_DIAGNOSTIC frame (safety-to-firmware)

| Offset | Size | Field | Type | Meaning |
| --- | --- | --- | --- | --- |
| 0 | 2 B | `sof` | uint16 | diagnostic frame sync distinct from the Jetson link |
| 2 | 2 B | `version_flags` | uint16 | protocol version in bits 7:0 and live safety input/output flags in bits 15:8 |
| 4 | 4 B | `sequence` | uint32 | monotonic diagnostic counter |
| 8 | 4 B | `timestamp_ms` | uint32 | ATmega128A monotonic milliseconds |
| 12 | 2 B | `state_cause` | uint16 | safety state in bits 7:0 and latched trip cause in bits 15:8 |
| 14 | 2 B | `crc16` | uint16 | CRC over sof through state_cause inclusive |

## Canonical packet example

HEALTH sequence 0, timestamp 0, `APP_ALIVE|LINK_HEALTHY` (little-endian):

`55 aa 02 00 00 00 00 00 00 00 00 00 11 00 1a ed`

## CRC

`CRC-16/CCITT-FALSE` - polynomial 0x1021, init 0xFFFF, reflect in False, reflect out False, xorout 0x0000, check value 0x29B1.

Coverage: sof-through-payload.

This resolves TBD-001. The ICD left polynomial, init, reflection and xorout open. CRC-16/CCITT-FALSE is fixed here because it is table-free on ATmega128A, unambiguous, and has a published check value.

## Timing

| Constant | Value (ms) | Owner | Purpose |
| --- | --- | --- | --- |
| `HEARTBEAT_TOGGLE_MS` | 100 | stm32 | PB8 toggle period while the Jetson link and local acquisition are both healthy |
| `HEARTBEAT_TIMEOUT_MS` | 500 | avr | the AVR trips the relay when no heartbeat edge arrives within this window |
| `JETSON_KEEPALIVE_TIMEOUT_MS` | 180 | stm32 | one missed 100 ms HEALTH plus 80 ms jitter stops PB8; combined with the 500 ms AVR timeout this leaves 20 ms of the 700 ms target for relay response |
| `JETSON_LINK_HEALTHY_MAX_AGE_MS` | 128 | jetson | one 128 ms TELEMETRY period may elapse before HEALTH clears LINK_HEALTHY |
| `INFERENCE_WORKER_STALL_MS` | 100 | jetson | clear LINK_HEALTHY when one inference or presentation callback exceeds the application-liveness deadline |
| `INFERENCE_P95_TARGET_MS` | 30 | jetson | target p95 model callback latency on the deployed Jetson |
| `PACKET_TO_UI_P95_TARGET_MS` | 100 | jetson | target p95 from validated packet receipt to Qt render |
| `CONDITION_WARNING_TARGET_MS` | 1500 | jetson | target from a persistent abnormal condition to advisory WARNING |
| `UI_HEARTBEAT_TIMEOUT_MS` | 300 | jetson | clear LINK_HEALTHY when a bound Qt main loop misses three 100 ms refresh heartbeats |
| `AVR_WATCHDOG_TIMEOUT_MS` | 500 | avr | reset a stalled safety main loop early enough to preserve the 700 ms relay-off target |
| `AVR_RESET_TO_SAFE_MS` | 700 | avr | worst case reset to relay OFF budget |
| `SUPERVISION_FAILURE_TO_RELAY_TARGET_MS` | 700 | system | target from Jetson or link supervision failure to physical relay OFF; physical response requires HIL measurement |
| `FRAME_PERIOD_MS` | 128 | stm32 | 128 samples at 1000 S/s |
| `INPUT_DEBOUNCE_MS` | 20 | avr | ARM and E-stop debounce window |
| `AVR_DIAGNOSTIC_PERIOD_MS` | 100 | avr | non-blocking safety diagnostic publication period |
| `AVR_DIAGNOSTIC_TIMEOUT_MS` | 300 | stm32 | maximum age for exposing AVR diagnostic status to Jetson |
| `STM_DIAGNOSTIC_PERIOD_MS` | 1000 | stm32 | read-only STM32 reliability counter publication period |
| `STM_DIAGNOSTIC_TIMEOUT_MS` | 3000 | jetson | maximum age for presenting STM32 reliability counters |
| `MOTOR_COMMAND_PERIOD_MS` | 100 | jetson | periodic refresh of the latest operator or experiment motor request |
| `MOTOR_COMMAND_TIMEOUT_MS` | 300 | stm32 | fail-safe PWM stop when no fresh valid motor request is received |

## Telemetry status flags

| Flag | Bit | Meaning |
| --- | --- | --- |
| `SENSOR_READY` | 0 | MPU-6050 and ACS712 acquisition are calibrated and ready |
| `LOCAL_FAULT` | 1 | a sticky local acquisition, peripheral or buffer fault is present |
| `MOTOR_ACTIVE` | 2 | the requested PWM duty is non-zero; this is not relay feedback |
| `AVR_DIAG_VALID` | 3 | a fresh CRC-valid AVR diagnostic frame is available |
| `AVR_ARMED` | 4 | the diagnostic safety state is ARMED |
| `AVR_TRIPPED` | 5 | the diagnostic safety state is TRIPPED |
| `AVR_ESTOP` | 6 | the latched diagnostic trip cause is E-stop |
| `AVR_HEARTBEAT_TIMEOUT` | 7 | the latched diagnostic trip cause is heartbeat timeout |

Status invariants:

- Reserved status bits are rejected.
- AVR state and cause bits require AVR_DIAG_VALID.
- AVR_ARMED and AVR_TRIPPED are mutually exclusive.
- AVR_ESTOP and AVR_HEARTBEAT_TIMEOUT are mutually exclusive and require AVR_TRIPPED.
- MOTOR_ACTIVE requires SENSOR_READY without LOCAL_FAULT.

## AVR diagnostic live status flags

| Flag | Bit | Meaning |
| --- | --- | --- |
| `HEARTBEAT_HEALTHY` | 0 | two supervised heartbeat edges have been observed and the latest edge is within timeout |
| `ARM_ACTIVE` | 1 | the debounced ARM input is active and the raw input has not released |
| `ESTOP_ACTIVE` | 2 | the raw or debounced E-stop input is active |
| `INTERNAL_HEALTHY` | 3 | the AVR application reports no internal fault |
| `RELAY_COMMAND` | 4 | the AVR is commanding its relay output active; this is not contact feedback |

## STM diagnostic status flags

| Flag | Bit | Meaning |
| --- | --- | --- |
| `KEEPALIVE_VALID` | 0 | a fresh CRC-valid Jetson HEALTH frame explicitly reports LINK_HEALTHY |
| `ACQUISITION_OVERRUN` | 1 | the acquisition block has overrun |
| `LOCAL_HEALTHY` | 2 | the local acquisition path is healthy |
| `HEARTBEAT_LEVEL` | 3 | the current supervised heartbeat GPIO command level |
| `AVR_DIAG_VALID` | 4 | the bridged AVR live status is from a fresh CRC-valid diagnostic frame |
| `AVR_HEARTBEAT_HEALTHY` | 5 | the AVR currently considers the supervised heartbeat healthy |
| `AVR_ARM_ACTIVE` | 6 | the AVR currently sees its ARM input active |
| `AVR_ESTOP_ACTIVE` | 7 | the AVR currently sees its E-stop input active |
| `AVR_INTERNAL_HEALTHY` | 8 | the AVR application currently reports no internal fault |
| `AVR_RELAY_COMMAND` | 9 | the AVR currently commands its relay output active; this is not contact feedback |

## Safety states

| State | Value | Relay | Meaning |
| --- | --- | --- | --- |
| DISARMED | 0 | off | power-on and post-reset state |
| ARMED | 1 | on | manual ARM asserted AND heartbeat healthy AND E-stop inactive AND no internal fault |
| TRIPPED | 2 | off | latched; requires manual re-arm |

## Motor directions

| Direction | Value | Meaning |
| --- | --- | --- |
| STOP | 0 | both bridge inputs low |
| FORWARD | 1 | PB6 PWM with PB7 held low |

## Signals

| Signal | From | To | Trigger | Level | Note |
| --- | --- | --- | --- | --- | --- |
| HEARTBEAT | STM32F411 PB8 | ATmega128A PE4 (INT4) | both | 3.3V push-pull into a 5V input | Supervised. AVR PE4 internal pull-up prevents a floating input while STM32 PB8 is high-impedance at reset. Toggled only while the Jetson keepalive and local acquisition health are both valid. An unconditional timer toggle would not detect Jetson failure and is forbidden. |
| ESTOP | SW2 switch | ATmega128A PD6 | level-low | 5V with internal pull-up, switch to GND | Prototype input. Real machinery requires a certified E-stop and safety relay. |
| ARM | SW1 momentary | ATmega128A PD5 | falling | 5V with internal pull-up, switch to GND | Manual re-arm only. Never asserted by software. |
| RELAY | ATmega128A PD7 | K1 IN | level-high | 5V logic | R2 bias must hold the relay de-energized while PD7 is tri-stated during reset. Trigger polarity must be measured before motor power is applied. |

## Pin map

| Board | Pin | Function | Connects to | Level | Note |
| --- | --- | --- | --- | --- | --- |
| STM32F411 | PB10 | I2C2_SCL | GY-521 SCL | 3.3V | AF4 open-drain; use the module pull-up only if it is tied to 3.3V |
| STM32F411 | PB3 | I2C2_SDA | GY-521 SDA | 3.3V | AF9 open-drain; NUCLEO-F411RE D3/CN10-31; keep the cable short |
| STM32F411 | PB1 | EXTI1 | GY-521 INT | 3.3V | MPU-6050 data-ready rising edge |
| GY-521 | AD0 | I2C_ADDRESS_SELECT | GND | 3.3V | selects MPU-6050 address 0x68 |
| STM32F411 | PA0 | ADC1_IN0 | ACS712 OUT via 12k series and 22k to GND | 0-3.3V analog | ADC DMA; add 100nF PA0-to-GND; divider limits a 5V output to 3.24V |
| STM32F411 | PB6 | TIM4_CH1 | LR7843 module PWM/IN | 3.3V PWM | 1kHz active-high; selected LR7843 module accepts 3.3V input |
| STM32F411 | PB7 | GPIO_OUT | NC | 3.3V | held LOW; LR7843 is a one-direction low-side switch, not an H-bridge |
| STM32F411 | PA2 | USART2_TX | Jetson J12 pin 10 RX | 3.3V TTL | 921600 8N1, TELEMETRY frames |
| STM32F411 | PA3 | USART2_RX | Jetson J12 pin 8 TX | 3.3V TTL | 921600 8N1, HEALTH frames |
| STM32F411 | PA9 | USART1_TX | ATmega128A PE0 RXD0 | 3.3V into a 5V input | diagnostics only; never a safety command |
| STM32F411 | PA10 | USART1_RX | ATmega128A PE1 TXD0 | 5V TTL | LEVEL SHIFT OR DIVIDER REQUIRED; a 5V output drives a 3.3V pin |
| STM32F411 | PB8 | GPIO_OUT | ATmega128A PE4 INT4 | 3.3V into a 5V input | supervised heartbeat, 100 ms toggle |
| STM32F411 | PB9 | RESERVED | no connection | do not drive | reserved by pinmap v4; AI abnormal must never become a direct relay trip output |
| ATmega128A | PE4 | INT4 | STM32 PB8 | 5V input | heartbeat edge capture with internal pull-up reset bias; INT4 to INT7 avoid the USART0 and TWI pins |
| ATmega128A | PE0 | RXD0 | STM32 PA9 | 5V input | diagnostic UART receive |
| ATmega128A | PE1 | TXD0 | STM32 PA10 | 5V output | diagnostic UART transmit; requires level shifting toward the STM32 |
| ATmega128A | PD5 | GPIO_IN_PULLUP | SW1 manual ARM | 5V | switch to GND |
| ATmega128A | PD6 | GPIO_IN_PULLUP | SW2 E-stop | 5V | switch to GND; active low |
| ATmega128A | PD7 | GPIO_OUT | K1 relay module IN | 5V | must leave the relay de-energized while tri-stated at reset; see R2 bias |
| ATmega128A | PB1 | ISP_SCK | ISP programmer | 5V | reserved for in-system programming |
| ATmega128A | PB2 | ISP_MOSI | ISP programmer | 5V | reserved for in-system programming |
| ATmega128A | PB3 | ISP_MISO | ISP programmer | 5V | reserved for in-system programming |
| Jetson Orin Nano | J12 pin 10 | UART RX | STM32 PA2 | 3.3V TTL | 3.3V only; never connect a 5V UART |
| Jetson Orin Nano | J12 pin 8 | UART TX | STM32 PA3 | 3.3V TTL | HEALTH keepalive and commands |
| Jetson Orin Nano | J12 pin 6 | GND | common signal ground | GND | the Jetson uses its own official PSU; signal ground only |

