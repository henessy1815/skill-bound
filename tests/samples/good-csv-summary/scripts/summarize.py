"""CSV 파일의 행 수와 열 이름을 출력한다."""
import csv
import sys


def summarize(path):
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        count = sum(1 for _ in reader)
    return header, count


def main():
    if len(sys.argv) != 2:
        print("usage: summarize.py <file.csv>")
        return 1
    header, count = summarize(sys.argv[1])
    print("columns:", ", ".join(header))
    print("rows:", count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
