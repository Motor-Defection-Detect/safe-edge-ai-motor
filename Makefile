# [읽기 가이드] 이 Makefile은 '검증 진입점 모음'이다. 처음엔 아래 세 줄만 이해하면 된다.
#   make contract  : 계약(JSON)과 생성 파일이 어긋나지 않았는지 검사
#   make host-unit : 하드웨어 없이 도는 Python 단위 테스트
#   make integration : C 와 Python 결과가 같은지 비교하는 통합 테스트
# 읽기 순서: 핵심 읽기 순서(1~13번) 밖의 보조 파일. 다음에 볼 곳: scripts/r300/link_contract.py, tests/
# 변수는 `make 변수=값` 으로 덮어쓸 수 있다 (예: make PYTHON=python3.11 host-unit).
# R300 AI Motor - verification entry points.
#
# Every target here is called by .claude/embedded/project.json. Nothing in this
# file touches hardware: flashing, relay control and Jetson deployment are
# approval boundaries and live in the hil gate, not here.

# 도구 이름 기본값. `?=` 는 환경에서 이미 정해졌으면 그 값을 그대로 쓴다는 뜻이다.
PYTHON ?= python3
ARM_CC ?= arm-none-eabi-gcc
AVR_CC ?= avr-gcc
AVR_BOARD_FLAGS ?= -DR300_AVR_CPU_HZ=16000000UL -DR300_RELAY_ACTIVE_HIGH=1

# .PHONY: 이 이름들은 파일이 아니라 '작업 이름'이다 (같은 이름의 파일이 있어도 항상 실행).
.PHONY: generate-contract contract host-unit firmware-headers firmware-logic qt-hmi metrics integration model-synthetic clean

## Regenerate every consumer from contracts/r300-link.json.
generate-contract:
	$(PYTHON) scripts/r300/link_contract.py generate

## Fail if any generated consumer drifted from the contract.
contract:
	$(PYTHON) scripts/r300/link_contract.py validate

## Host unit tier: pure-Python invariants, no compiler and no board.
host-unit:
	$(PYTHON) -m pytest tests/unit -q

## Both firmware toolchains must accept the generated header with warnings as errors.
firmware-headers:
	@set -e; \
	printf '#include "r300_link.h"\nuint16_t p(const uint8_t *d, size_t n){return r300_crc16(d,n);}\nconst uint32_t f = R300_TELEMETRY_BYTES(128);\n' \
	  | $(ARM_CC) -mcpu=cortex-m4 -mthumb -std=c11 -Wall -Wextra -Werror -Os \
	      -c -I firmware/stm32/include -o /tmp/r300_arm_probe.o -x c -; \
	echo "  arm-none-eabi-gcc OK"; \
	printf '#include "r300_link.h"\nuint16_t p(const uint8_t *d, size_t n){return r300_crc16(d,n);}\nconst uint16_t t = R300_HEARTBEAT_TIMEOUT_MS;\n' \
	  | $(AVR_CC) -mmcu=atmega128 -std=c11 -Wall -Wextra -Werror -Os \
	      -c -I firmware/avr/include -o /tmp/r300_avr_probe.o -x c -; \
	echo "  avr-gcc (atmega128) OK"

## Compile the hardware-free protocol and safety state machines for their targets.
firmware-logic:
	$(ARM_CC) -mcpu=cortex-m4 -mthumb -std=c11 -Wall -Wextra -Werror -Os -I firmware/stm32/include -c firmware/stm32/r300_protocol.c -o /tmp/r300_stm32_protocol.o
	$(ARM_CC) -mcpu=cortex-m4 -mthumb -std=c11 -Wall -Wextra -Werror -Os -I firmware/stm32/include -c firmware/stm32/r300_controller.c -o /tmp/r300_stm32_controller.o
	$(ARM_CC) -mcpu=cortex-m4 -mthumb -std=c11 -Wall -Wextra -Werror -Os -I firmware/stm32/include -c firmware/stm32/r300_board_config.c -o /tmp/r300_stm32_board_config.o
	$(ARM_CC) -mcpu=cortex-m4 -mthumb -std=c11 -Wall -Wextra -Werror -Os -I firmware/stm32/include -c firmware/stm32/r300_stm32_runtime.c -o /tmp/r300_stm32_runtime.o
	$(ARM_CC) -mcpu=cortex-m4 -mthumb -std=c11 -Wall -Wextra -Werror -Os -I firmware/stm32/include -c firmware/stm32/r300_stm32_platform.c -o /tmp/r300_stm32_platform.o
	$(ARM_CC) -mcpu=cortex-m4 -mthumb -std=c11 -Wall -Wextra -Werror -Os -I firmware/stm32/include -c firmware/stm32/r300_stm32_main.c -o /tmp/r300_stm32_main.o
	$(ARM_CC) -mcpu=cortex-m4 -mthumb -std=c11 -Wall -Wextra -Werror -Os -ffunction-sections -fdata-sections -I firmware/stm32/include -nostartfiles -Wl,--gc-sections,-Map=/tmp/r300_stm32.map,-Tfirmware/stm32/stm32f411re.ld firmware/stm32/startup_stm32f411.c firmware/stm32/r300_protocol.c firmware/stm32/r300_controller.c firmware/stm32/r300_board_config.c firmware/stm32/r300_stm32_runtime.c firmware/stm32/r300_stm32_platform.c firmware/stm32/r300_stm32_main.c -lc -lgcc -lnosys -o /tmp/r300_stm32.elf
	$(AVR_CC) -mmcu=atmega128 $(AVR_BOARD_FLAGS) -std=c11 -Wall -Wextra -Werror -Os -I firmware/avr/include -c firmware/avr/r300_safety.c -o /tmp/r300_avr_safety.o
	$(AVR_CC) -mmcu=atmega128 $(AVR_BOARD_FLAGS) -std=c11 -Wall -Wextra -Werror -Os -I firmware/avr/include -c firmware/avr/r300_avr_diagnostics.c -o /tmp/r300_avr_diagnostics.o
	$(AVR_CC) -mmcu=atmega128 $(AVR_BOARD_FLAGS) -std=c11 -Wall -Wextra -Werror -Os -I firmware/avr/include -c firmware/avr/r300_avr_platform.c -o /tmp/r300_avr_platform.o
	$(AVR_CC) -mmcu=atmega128 $(AVR_BOARD_FLAGS) -std=c11 -Wall -Wextra -Werror -Os -I firmware/avr/include -c firmware/avr/r300_avr_runtime.c -o /tmp/r300_avr_runtime.o
	$(AVR_CC) -mmcu=atmega128 $(AVR_BOARD_FLAGS) -std=c11 -Wall -Wextra -Werror -Os -I firmware/avr/include -c firmware/avr/r300_avr_main.c -o /tmp/r300_avr_main.o
	$(AVR_CC) -mmcu=atmega128 $(AVR_BOARD_FLAGS) -std=c11 -Wall -Wextra -Werror -Os -I firmware/avr/include firmware/avr/r300_safety.c firmware/avr/r300_avr_diagnostics.c firmware/avr/r300_avr_platform.c firmware/avr/r300_avr_runtime.c firmware/avr/r300_avr_main.c -o /tmp/r300_avr_safety.elf

## Exercise the PyQt5 dashboard without opening a desktop window.
qt-hmi:
	QT_QPA_PLATFORM=offscreen $(PYTHON) -m pytest tests/qt -q

## Measure what the compilers actually emit (no -Werror) for the warning budget.
metrics:
	$(PYTHON) scripts/r300/build_metrics.py

## Integration tier: the C and Python renderings of the contract must agree.
integration:
	$(PYTHON) -m pytest tests/integration -q

## Train a demo-only model; real accuracy still requires physical session artifacts.
model-synthetic:
	$(PYTHON) scripts/r300/train_synthetic_model.py

clean:
	rm -f /tmp/r300_arm_probe.o /tmp/r300_avr_probe.o /tmp/r300_stm32_protocol.o /tmp/r300_stm32_controller.o /tmp/r300_stm32_board_config.o /tmp/r300_stm32_runtime.o /tmp/r300_stm32_platform.o /tmp/r300_stm32_main.o /tmp/r300_stm32.elf /tmp/r300_stm32.map /tmp/r300_avr_safety.o /tmp/r300_avr_diagnostics.o /tmp/r300_avr_platform.o /tmp/r300_avr_runtime.o /tmp/r300_avr_main.o /tmp/r300_avr_safety.elf
