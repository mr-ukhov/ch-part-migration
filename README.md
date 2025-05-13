# ch-part-migration. Независимая миграция партиций между нодами ClickHouse кластера
В ClickHouse нет встроенной возможности не имея доступ в инфраструктуру перенести одну партицию таблицы на другую ноду 
кластера в одинаковую по структуре таблицу.  
Проект(непосредственно даг [migrator.py](migrator.py)) позволяет перенести отдельную партицию любой партиционированной 
таблицы семейства MergeTree на другую ноду(список нод) кластера без доступа в инфраструктуру (clickhouse-client) минуя 
ресурсозатратный процесс расжатия и сжатия при `INSERT SELECT`, т.е напрямую между нодами через реплицируемые таблицы
(ReplicatedMergeTree)

![Variant.jpg](images%2FVariant.jpg)

## Инструкция по развертыванию тестового стенда. Если есть свой стенд, пункт можно пропустить ([Описание работы дага](#dag-description))
Находясь в корне проекта выполнить следующие команды:

1. Развернуть кластер ClickHouse v25.1.5.31 и Clickhouse Keeper: `docker compose -f Docker/Clickhouse/docker-compose.yml up -d`
[docker-compose.yml](Docker%2FClickhouse%2Fdocker-compose.yml)

2. Развернуть Airflow v2.10.5: `docker compose -f Docker/Airflow/docker-compose.yaml up -d` 
[docker-compose.yaml](Docker%2FAirflow%2Fdocker-compose.yaml)

3. Подключиться к Clickhouse, например через DBeaver:  
   Хост - `localhost`  
   Порт - `8123` для первой ноды. Для остальных - `8124`(2) / `8125`(3) / `8126`(4)  
   Пользователь - `default`  
   Пароль - `11111`

4. Создать таблицу с исходными данными:  
    ```sql 
    CREATE TABLE trips (
        trip_id             UInt32,
        pickup_datetime     DateTime,
        dropoff_datetime    DateTime,
        pickup_longitude    Nullable(Float64),
        pickup_latitude     Nullable(Float64),
        dropoff_longitude   Nullable(Float64),
        dropoff_latitude    Nullable(Float64),
        passenger_count     UInt8,
        trip_distance       Float32,
        fare_amount         Float32,
        extra               Float32,
        tip_amount          Float32,
        tolls_amount        Float32,
        total_amount        Float32,
        payment_type        Enum('CSH' = 1, 'CRE' = 2, 'NOC' = 3, 'DIS' = 4, 'UNK' = 5),
        pickup_ntaname      LowCardinality(String),
        dropoff_ntaname     LowCardinality(String)
    )
    ENGINE = MergeTree
    ORDER BY (pickup_datetime, dropoff_datetime)
    PARTITION BY toYYYYMM(pickup_datetime);
    ```

5. Наполнить таблицу исходными данными
    ```sql
   INSERT INTO trips
   SELECT
       trip_id,
       pickup_datetime,
       dropoff_datetime,
       pickup_longitude,
       pickup_latitude,
       dropoff_longitude,
       dropoff_latitude,
       passenger_count,
       trip_distance,
       fare_amount,
       extra,
       tip_amount,
       tolls_amount,
       total_amount,
       payment_type,
       pickup_ntaname,
       dropoff_ntaname
   FROM s3(
       'https://datasets-documentation.s3.eu-west-3.amazonaws.com/nyc-taxi/trips_{0..2}.gz',
       'TabSeparatedWithNames'
   ); 
   ```

6. Подключиться к интерфейсу Airflow http://localhost:8080/  
   Логин - `airflow`  
   Пароль - `airflow`  

7. Создать конекшены до нод кликхауса:  
![1.png](images%2F1.png)

8. В интерфейсе Airflow перейти к дагу `migrator`

<a id="dag-description"></a>
## Описание работы дага [migrator.py](migrator.py)

**Даг необходимо запускать с конфигом**. Параметры дага:
1. `source_conn_id` - название конекшена к исходной ноде, с которой будем экспортировать партиции
2. `source_database` - БД на исходной ноде, в которой находится исходная таблица
3. `source_table` - название исходной таблицы, из которой будут экспортированы партиции
4. `source_partitions` - список с названиями партиций для переноса. Если первым в списке будет значение "all", 
   то перенесутся все партиции таблицы, т.е. вся таблица
5. `target_nodes_conn_id` - список конекшенов к целевым нодам, на которые нужно перенести данные.
6. `target_database` - в какую БД сохранить результат на целевых нодах
7. `target_table` - название целевой таблицы в которую будут перенесены партиции. Этой таблицы может и не существовать,
   даг сам возьмет DDL оригинальной таблицы (`source_table`) и создаст аналогичную по структуре таблицу в БД 
   `target_database` с названием `target_table`.

**Алгоритм работы:**
1. Сохранение DDL исходной таблицы
2. Создание реплицируемых таблиц на нодах(исходная+целевые) с аналогичной схемой, как у оригинальной.
   Таблицы создаются с именем исходной таблицы и префиксом `tmp_repl__`
3. Приаттачивание необходимых партиций из исходной таблицы к реплицируемой (`ATTACH PARTITION FROM`)
4. Ожидание завершения репликации между нодами
5. Создание целевой таблицы на целевых нодах
6. Приаттачивание перенесенных партиций к целевой таблице из реплицируемых (`ATTACH PARTITION FROM`)
7. Удаление реплицируемых таблиц

Пример запуска со стандартными параметрами. На изображении ниже будут импортированы две партиции (201507, 201508) из 
таблицы `default.trips` ноды `ch_node_1`. Партиции будут перенесены на ноды `ch_node_2` и `ch_node_3` и
сохранены в таблицу `default.tmp__trips`
![2.png](images%2F2.png)
![3.png](images%2F3.png)
![6.png](images%2F6.png)


## Дополнительно. Мониторинг репликации с использованием Apache Superset

1. Развернуть Apache Superset `docker compose -f Docker/Superset/docker-compose-image-tag.yml up -d`
2. Внутри контейнера установить библиотеку для подключения BI к кликхаусу `pip install clickhouse-connect`
3. Подключить основную ноду кликхауса ![4.png](images%2F4.png)
4. Подготовить датасет с запросом:
   ```sql
    SELECT 
        query,
        CASE 
            WHEN query ILIKE '%ALTER TABLE%.tmp_repl__%ATTACH PARTITION%' THEN 'init_query'
            WHEN query ILIKE '%ALTER TABLE%ATTACH PARTITION%FROM%.tmp_repl__%' THEN 'node_query'
        END AS query_type,
        
        CASE 
            WHEN query_type = 'init_query'
            THEN regexpExtract(query, '(?i)FROM\\s+([^\\s]+)', 1)
        END AS source_table,
        
        CASE 
            WHEN query_type = 'node_query'
            THEN regexpExtract(query, '(?i)ALTER TABLE\\s+([^\\s]+)', 1)
        END AS target_table,
        
        CASE 
            WHEN query_type = 'init_query'
            THEN regexpExtract(query, '(?i)ALTER TABLE\\s+([^\\s]+)', 1)
            WHEN query_type = 'node_query'
            THEN regexpExtract(query, '(?i)FROM\\s+([^\\s]+)', 1)
        END AS replicate_table,

        regexpExtract(replicate_table, '(?i)tmp_repl__([^\\s]+)', 1) AS only_table_name,
        regexpExtract(query, '(?i)ATTACH PARTITION\\s+([^\\s]+)', 1) AS `partition`,
        query_start_time,
        event_date
    
    FROM clusterAllReplicas(all_sharded,system.query_log) 
    WHERE 
        query ILIKE '%tmp_repl__%'
        AND query ILIKE '%ATTACH PARTITION%'
        AND query NOT ILIKE '%system.query_log%'
        AND `type` = 'QueryFinish'
        AND query_kind = 'Alter'
    
    ORDER BY query_start_time
   ```

5. На основе такого датасета можно построить различные визуализации
![5.png](images%2F5.png)