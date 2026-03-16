from pyspark.sql import SparkSession
import time

def main():
    spark = (
        SparkSession.builder
        .appName("dbm_proj1_smoke_test")
        .getOrCreate()
    )

    n = spark.range(0, 5000000).count()
    print(f"TEST COUNT = {n}")
    time.sleep(60)

    spark.stop()

if __name__ == "__main__":
    main()
