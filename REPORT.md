# Project 2: Streaming Lakehouse Pipeline

## 1. Medallion layer schemas

### Bronze

**Schema**
```text
root
 |-- vendor_id: string (nullable = true)
 |-- raw_json: string (nullable = true)
 |-- kafka_timestamp: timestamp (nullable = true)
 ```
 The Bronze layer stores the raw Kafka event payload almost unchanged. Each message is written as a JSON string in raw_json, alongside a Kafka ingestion timestamp and vendor key. 
### Silver

**Schema**
```text
root
 |-- DOLocationID: integer (nullable = true)
 |-- PULocationID: integer (nullable = true)
 |-- VendorID: integer (nullable = true)
 |-- tpep_pickup_datetime: string (nullable = true)
 |-- tpep_dropoff_datetime: string (nullable = true)
 |-- passenger_count: double (nullable = true)
 |-- trip_distance: double (nullable = true)
 |-- fare_amount: double (nullable = true)
 |-- tip_amount: double (nullable = true)
 |-- total_amount: double (nullable = true)
 |-- payment_type: integer (nullable = true)
 |-- congestion_surcharge: double (nullable = true)
 |-- kafka_timestamp: timestamp (nullable = true)
 |-- pickup_datetime: timestamp (nullable = true)
 |-- dropoff_datetime: timestamp (nullable = true)
 |-- pickup_zone: string (nullable = true)
 |-- dropoff_zone: string (nullable = true)
 |-- is_weekend: boolean (nullable = true)
 ```

Silver changes the Bronze data in three major ways:  
1. The raw JSON is parsed into typed trip columns.
2. Pickup and dropoff timestamps are cast into proper timestamp columns.
3. The data is cleaned and enriched with lookup values.

Compared to Bronze, Silver still keeps trip-level detail, but it adds structure and business meaning. The custom scenario column is_weekend is also created here from the pickup timestamp.

### Gold


**Schema**
```text
root
 |-- window_start: timestamp (nullable = true)
 |-- pickup_zone: string (nullable = true)
 |-- is_weekend: boolean (nullable = true)
 |-- trip_count: long (nullable = true)
 |-- avg_total_amount: double (nullable = true)
 ```

Gold contains hourly aggregated analytics rather than individual taxi trips. The aggregation groups records by:
* hourly pickup window
* pickup zone
* weekend/weekday flag

For each group, the pipeline calculates:
* trip_count
* avg_total_amount

Gold is much smaller and more useful for reporting than Silver.

## 2. Cleaning rules and enrichment

### Cleaning rules

The following cleaning rules were applied in the Silver layer:

* **Filter rows where `fare_amount <= 0`**
  Trips with zero or negative fare are treated as invalid for this analysis.

* **Filter rows where `PULocationID` is null**
  Pickup location is required both for geographic analysis and for the zone enrichment join.

* **Cast `tpep_pickup_datetime` and `tpep_dropoff_datetime` into timestamps**
  The producer sends datetime fields as strings inside JSON, so they must be converted into timestamps for time-based analytics and windowing.

* **Deduplication using a business key**
  Duplicate trip rows are removed using:

  * `VendorID`
  * `pickup_datetime`
  * `PULocationID`

  This is a business-level deduplication rule intended to reduce repeated trip records in the cleaned layer.

### Enrichment

The Silver layer joins the trip records with the static taxi zone lookup table:

* `PULocationID` → `pickup_zone`
* `DOLocationID` → `dropoff_zone`

This enrichment adds readable zone names to the trip data, making the Gold analytics easier to interpret than raw numeric location IDs.


## 3. Streaming configuration

### Checkpoint path and what it stores

Each streaming query has its own checkpoint directory:
* Bronze: `/home/jovyan/project/checkpoints/bronze`
* Silver: `/home/jovyan/project/checkpoints/silver`
* Gold: `/home/jovyan/project/checkpoints/gold`

These checkpoints store Structured Streaming progress and state. In particular, they keep the information needed for each query to resume correctly after restart. For the Kafka source, this includes the source progress already processed by the pipeline. For stateful transformations, checkpointing also preserves execution state between runs.

### Trigger interval and why it was chosen

The trigger intervals are:

* Bronze: `10 seconds`
* Silver: `10 seconds`
* Gold: `20 seconds`

Bronze and Silver use 10-second micro-batches to keep ingestion and cleaning responsive. Gold uses a 20-second trigger because aggregation does not need to update as frequently.

### Output mode and why

All three queries use **append** mode.
* Bronze uses append because new raw Kafka events are continuously added.
* Silver uses append because cleaned trip rows are written as new records.
* Gold uses append because new aggregated hourly results are appended as streaming output.

### Watermark and why

A watermark is used in the Silver layer:

```python
.withWatermark("pickup_datetime", "10 minutes")
```

In Silver layer we use it to not check for duplicates indefinitely. This also affects Gold layer since in append mode a window only writes to the Gold table when the watermark passes the end of that window.


## 4. Gold table partitioning strategy

The Gold table is partitioned by day of the hourly window start time, because Gold queries could primarily be time based aggregates and filters. Another option we considered would be pickup_zone but that could create small partitions.

### Iceberg snapshot history


```sql
 SELECT committed_at, snapshot_id, operation 
    FROM lakehouse.taxi.gold.snapshots 
    ORDER BY committed_at DESC 
    LIMIT 5
```


|committed_at           |snapshot_id        |operation|
| ----------------------- | ------------------: | --------- |
|2026-04-04 11:33:40.812|6357791687888977761|append   |
|2026-04-04 11:33:20.854|1898439055939937471|append   |
|2026-04-04 11:33:00.849|5856993993000763348|append   |
|2026-04-04 11:32:40.81 |4223660752375950876|append   |
|2026-04-04 11:32:20.806|6207475724948638494|append   |


## 5. Restart proof

To test restart correctness, the pipeline was run until all three layers contained data. Row counts were recorded, the streaming queries were stopped, and then restarted using the same checkpoint directories. The Kafka producer was not run during the restart window.

*Note on Draining:* After stopping the producer, the streams were left running for several minutes to "drain" any buffered or in-flight data. This ensured all records were fully committed to the Iceberg tables before the "Before restart" counts were captured, preventing artificial row count jumps during the restart phase.

### Row counts before and after restart

| Layer  | Before restart | After restart |
| ------ | -------------: | ------------: |
| Bronze |         548148 |        548148 |
| Silver |          526271 |         526271 |
| Gold   |           16075 |          16075 |

Because the counts did not change after restart, the pipeline resumed from checkpoint state without duplicating previously processed data.



## 6. Custom scenario

The custom scenario required adding a boolean column `is_weekend`:
* `true` if the pickup date is Saturday or Sunday
* `false` otherwise
This was implemented in the Silver layer with:

```python
.withColumn("is_weekend", F.dayofweek("pickup_datetime").isin([1, 7]))
```

[Spark’s](https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.functions.dayofweek.html) `dayofweek()` function returns:
* `1` for Sunday
* `7` for Saturday
So the expression creates a boolean column that flags weekend trips.
The Gold layer keeps `is_weekend` as an aggregation dimension, which allows weekend and weekday trips to be analyzed separately.
The following query was used to compute the required comparison:

```sql
SELECT 
        is_weekend, 
        SUM(trip_count) AS total_trips, 
        SUM(avg_total_amount * trip_count) / SUM(trip_count) AS overall_avg_amount
    FROM lakehouse.taxi.gold
    GROUP BY is_weekend
```

Observed output from the recorded run:

| is_weekend | total_trips | overall_avg_amount |
| ---------- | ----------: | -----------------: |
|     false|     349258|28.803761860859318|
|      true|     162596|   28.076160606657|



## 7. How to run

```bash
# Step 1: Start infrastructure
docker compose up -d

# Step 2: Start the producer
# Optional: Use --rate (e.g., --rate 100) to speed up data ingestion 
# and reach weekend records faster.
python produce.py --loop --rate 100

# Step 3: Run the pipeline
# Open the Jupyter notebook and run all cells
```

Additional notes:

* The taxi data files must be present in `data/`.
* The taxi zone lookup table must be available to the notebook.
* The notebook runs the Bronze, Silver, and Gold streaming queries end-to-end.
* The numerical outputs and row counts provided in this report represent a representative subset of the NYC Taxi dataset processed to verify pipeline logic and streaming integrity. They do not reflect the total volume of the entire dataset, as the primary goal was to validate the architecture and custom scenario functionality.

### `.env` values

add values required by compose.yml

```env
MINIO_ROOT_USER=<your_value>
MINIO_ROOT_PASSWORD=<your_value>
JUPYTER_TOKEN=<your_value>
```

