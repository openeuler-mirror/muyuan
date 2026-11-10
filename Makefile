PYTHON ?= python3
L1_DIR := benchmarks/vulnerability/zsoft-l1
DETECT_DIR := benchmarks/vulnerability/zsoft-detect

.PHONY: fetch-l1-sources test-detect test-l1-unit test-l1-samples test-l1-userspace-prepare test-l1-kernel-tcg test-l1-kernel-kvm

fetch-l1-sources:
	cd $(L1_DIR) && PYTHONPATH=src $(PYTHON) -m zsoft_poc fetch-sources --all

test-detect:
	cd $(DETECT_DIR) && $(PYTHON) scripts/validate_dataset.py
	cd $(DETECT_DIR) && $(PYTHON) scripts/build_baseline.py --check
	cd $(DETECT_DIR) && $(PYTHON) -m unittest discover -s tests -v

test-l1-unit:
	cd $(L1_DIR) && PYTHONPATH=src $(PYTHON) -m pytest tests/test_task.py tests/test_task_ops.py tests/test_runner.py tests/test_server.py tests/test_source_locks.py -q

test-l1-samples:
	cd $(L1_DIR) && PYTHONPATH=src $(PYTHON) -m pytest tests/test_sample_docker.py tests/test_sample_asan_docker.py tests/test_sample_web_docker.py -q

test-l1-userspace-prepare:
	cd $(L1_DIR) && PYTHONPATH=src $(PYTHON) -m zsoft_poc validate --strict
	cd $(L1_DIR) && PYTHONPATH=src $(PYTHON) scripts/prepare_userspace_tasks.py

test-l1-kernel-tcg:
	cd $(L1_DIR) && ZSOFT_KERNEL_ACCEL=tcg RUN_KERNEL_DOCKER_TESTS=1 PYTHONPATH=src $(PYTHON) -m pytest tests/test_linux_kernel_docker.py -q

test-l1-kernel-kvm:
	cd $(L1_DIR) && ZSOFT_KERNEL_ACCEL=kvm RUN_KERNEL_DOCKER_TESTS=1 PYTHONPATH=src $(PYTHON) -m pytest tests/test_linux_kernel_docker.py -q
