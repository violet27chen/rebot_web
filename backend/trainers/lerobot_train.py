"""Run the official trainer, keeping exact steps in its human-readable log."""
import re


def exact_step_formatter(original):
    def render(tracker):
        # LeRobot's default display rounds 9800 -> 10K, losing plot positions.
        # Change only the displayed step; all other official metrics stay intact.
        return re.sub(r'^step:\S+', 'step:' + str(tracker.steps), original(tracker), count=1)
    return render


def main():
    from lerobot.utils.logging_utils import MetricsTracker
    MetricsTracker.__str__ = exact_step_formatter(MetricsTracker.__str__)
    from lerobot.scripts.lerobot_train import main as official_main
    official_main()


if __name__ == '__main__':
    main()
