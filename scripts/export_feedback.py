"""Export all analyses and feedback to CSV using DATABASE_URL or local SQLite."""
import argparse
import csv
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import connection


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='CSV output path; must not already exist')
    args = parser.parse_args()
    with connection() as db:
        rows = db.execute('''SELECT a.id, a.created_at, a.original_log, a.response_json,
                             f.rating, f.feedback, f.updated_at
                             FROM analysis_records a LEFT JOIN analysis_feedback f ON f.analysis_id = a.id
                             ORDER BY a.created_at, a.id''').fetchall()
    with args.output.open('x', encoding='utf-8', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['analysis_id', 'created_at', 'original_log', 'response_json', 'rating', 'feedback', 'feedback_updated_at'])
        writer.writerows(rows)
    print(f'Exported {len(rows)} analyses to {args.output}')


if __name__ == '__main__':
    main()
