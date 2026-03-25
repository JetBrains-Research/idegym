#!/usr/bin/env python3
"""
Local Integration Test Runner for IdeGYM on Minikube.

This script orchestrates the complete testing workflow:
1. Builds required Docker images
2. Sets up Kubernetes environment in minikube
3. Runs integration tests
4. Optionally cleans up resources

Usage:
    python run_tests.py                    # Run all tests
    python run_tests.py --skip-build       # Skip image building
    python run_tests.py --reuse-resources  # Reuse existing K8s resources
    python run_tests.py --no-cleanup       # Don't clean up after tests
    python run_tests.py --test test_name   # Run specific test
"""

import argparse
import asyncio
import sys
from pathlib import Path

from idegym.utils.logging import get_logger
from utils.build_images import build_all_images
from utils.k8s_setup import (
    cleanup_kubernetes_environment,
    setup_kubernetes_environment,
)

logger = get_logger(__name__)


def run_pytest(
    test_name: str | None = None,
    delete_namespace: bool = False,
    delete_kustomize_services: bool = False,
) -> bool:
    """
    Run pytest with the specified test.

    Args:
        test_name: Optional specific test to run

    Returns:
        bool: True if tests passed, False otherwise
    """
    import subprocess

    module_dir = Path(__file__).parent
    tests_dir = module_dir / "tests"

    # Override root pytest config to avoid dependency on pytest-randomly
    cmd = [sys.executable, "-m", "pytest", str(tests_dir), "-v", "-s", "-o", "addopts="]

    if test_name:
        cmd.extend(["-k", test_name])

    if delete_namespace:
        cmd.append("--delete-namespace")

    if delete_kustomize_services:
        cmd.append("--delete-kustomize-services")

    logger.info("Running tests...")
    result = subprocess.run(cmd, check=False, cwd=module_dir)

    return result.returncode == 0


async def main_async() -> int:
    """Main async entry point."""
    parser = argparse.ArgumentParser(
        description="Local integration test runner for minikube",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--skip-build", action="store_true", help="Skip building Docker images")
    parser.add_argument(
        "--reuse-resources", action="store_true", help="Reuse existing Kubernetes resources instead of recreating"
    )
    parser.add_argument("--test", type=str, help="Run a specific test by name (uses pytest -k)")
    parser.add_argument(
        "--no-cleanup", action="store_true", help="Don't delete resources after tests (useful for debugging)"
    )
    parser.add_argument(
        "--clean-namespace", action="store_true", help="Recreate idegym-local namespace before tests (full cleanup)"
    )
    parser.add_argument(
        "--delete-namespace",
        action="store_true",
        help="Delete the entire idegym-local namespace after all tests complete",
    )
    parser.add_argument(
        "--delete-kustomize-services",
        action="store_true",
        help="Delete only services defined in kustomization.yaml after all tests complete",
    )

    args = parser.parse_args()

    try:
        # Step 1: Build images (unless skipped)
        if not args.skip_build:
            logger.info("=" * 80)
            logger.info("STEP 1: Building Docker Images")
            logger.info("=" * 80)
            build_all_images()
        else:
            logger.info("Skipping image builds")

        # Step 2: Set up Kubernetes environment
        logger.info("=" * 80)
        logger.info("STEP 2: Setting Up Kubernetes Environment")
        logger.info("=" * 80)

        if not setup_kubernetes_environment(reuse_resources=args.reuse_resources, clean_namespace=args.clean_namespace):
            logger.error("Failed to set up Kubernetes environment")
            return 1

        # Step 3: Run tests
        logger.info("=" * 80)
        logger.info("STEP 3: Running Integration Tests")
        logger.info("=" * 80)

        tests_successful = run_pytest(
            args.test,
            delete_namespace=args.delete_namespace,
            delete_kustomize_services=args.delete_kustomize_services,
        )

        if tests_successful:
            logger.info("=" * 80)
            logger.info("✓ ALL TESTS PASSED")
            logger.info("=" * 80)
            return 0
        else:
            logger.warning("=" * 80)
            logger.warning("✗ SOME TESTS FAILED")
            logger.warning("=" * 80)
            return 1

    except Exception as e:
        logger.error(f"Error during test execution: {e}", exc_info=True)
        return 1

    finally:
        # Cleanup (optional)
        if not args.no_cleanup and not (args.delete_namespace or args.delete_kustomize_services):
            try:
                logger.info("=" * 80)
                logger.info("CLEANUP: Removing Kubernetes Resources")
                logger.info("=" * 80)
                # --clean-namespace is a pre-test setup option; post-test cleanup removes deployed resources.
                cleanup_kubernetes_environment(clean_namespace=False)
            except Exception as cleanup_error:
                logger.error(f"Error during cleanup: {cleanup_error}", exc_info=True)
        elif args.delete_namespace or args.delete_kustomize_services:
            logger.info(
                "Skipping k8s_setup cleanup because post-test deletion is handled by pytest flags",
            )


def main() -> int:
    """Entry point for the script that runs the async main function."""
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
