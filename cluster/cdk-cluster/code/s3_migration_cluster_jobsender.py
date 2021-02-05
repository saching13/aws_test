# PROJECT LONGBOW - JOBSENDER FOR COMPARE AMAZON S3 AND CREATE DELTA JOB LIST TO SQS

import json
import os
import sys
import time
from configparser import ConfigParser
from s3_migration_lib import set_env, set_log, job_upload_sqs_ddb, delta_job_list, check_sqs_empty, \
    get_des_file_list, get_src_file_list
from operator import itemgetter
from pathlib import Path

# Read config.ini
cfg = ConfigParser()
try:
    file_path = os.path.split(os.path.abspath(__file__))[0]
    cfg.read(f'{file_path}/s3_migration_cluster_config.ini', encoding='utf-8-sig')
    table_queue_name = cfg.get('Basic', 'table_queue_name')
    sqs_queue_name = cfg.get('Basic', 'sqs_queue_name')
    ssm_parameter_bucket = cfg.get('Basic', 'ssm_parameter_bucket')
    ssm_parameter_credentials = cfg.get('Basic', 'ssm_parameter_credentials')
    LocalProfileMode = cfg.getboolean('Debug', 'LocalProfileMode')
    JobType = cfg.get('Basic', 'JobType')
    MaxRetry = cfg.getint('Mode', 'MaxRetry')
    MaxThread = cfg.getint('Mode', 'MaxThread')
    MaxParallelFile = cfg.getint('Mode', 'MaxParallelFile')
    LoggingLevel = cfg.get('Debug', 'LoggingLevel')
    JobsenderCompareVersionId = cfg.getboolean('Mode', 'JobsenderCompareVersionId')
except Exception as e:
    print("s3_migration_cluster_config.ini ERR: ", str(e))
    sys.exit(0)

# if CDK deploy, get para from environment variable
try:
    table_queue_name = os.environ['table_queue_name']
    sqs_queue_name = os.environ['sqs_queue_name']
    ssm_parameter_bucket = os.environ['ssm_parameter_bucket']
except Exception as e:
    print("No Environment Variable from CDK, use the para from config.ini", str(e))


# Main
if __name__ == '__main__':

    # Set Logging
    logger, log_file_name = set_log(LoggingLevel, 'jobsender')

    # Get Environment
    sqs, sqs_queue, table, s3_src_client, s3_des_client, instance_id, ssm = \
        set_env(JobType=JobType,
                LocalProfileMode=LocalProfileMode,
                table_queue_name=table_queue_name,
                sqs_queue_name=sqs_queue_name,
                ssm_parameter_credentials=ssm_parameter_credentials,
                MaxRetry=MaxRetry)

    #######
    # Program start processing here
    #######

    # Get ignore file list
    ignore_list_path = os.path.split(os.path.abspath(__file__))[0] + '/s3_migration_ignore_list.txt'
    ignore_list = []
    try:
        with open(ignore_list_path, 'r') as f:
            ignore_list = f.read().splitlines()
        logger.info(f'Found ignore files list Length: {len(ignore_list)}, in {ignore_list_path}')
    except Exception as e:
        if e.args[1] == 'No such file or directory':
            logger.info(f'No ignore files list in {ignore_list_path}')
            print(f'No ignore files list in {ignore_list_path}')
        else:
            logger.info(str(e))

    # Check SQS is empty or not
    if check_sqs_empty(sqs, sqs_queue):
        logger.info('Job sqs queue is empty, now process comparing s3 bucket...')

        # Load Bucket para from ssm parameter store
        logger.info(f'Get ssm_parameter_bucket: {ssm_parameter_bucket}')
        try:
            load_bucket_para = json.loads(ssm.get_parameter(Name=ssm_parameter_bucket)['Parameter']['Value'])
            logger.info(f'Recieved ssm {json.dumps(load_bucket_para)}')
        except Exception as e:
            logger.error(f'Fail to get buckets info from ssm_parameter_bucket, fix and restart Jobsender. {str(e)}')
            sys.exit(0)
        for bucket_para in load_bucket_para:
            src_bucket = bucket_para['src_bucket']
            src_prefix = bucket_para['src_prefix']
            des_bucket = bucket_para['des_bucket']
            des_prefix = bucket_para['des_prefix']

            # Get List on S3
            logger.info('Get source bucket')
            src_file_list = get_src_file_list(
                s3_client=s3_src_client,
                bucket=src_bucket,
                S3Prefix=src_prefix,
                JobsenderCompareVersionId=JobsenderCompareVersionId
            )
            logger.info('Get destination bucket')
            des_file_list = get_des_file_list(
                s3_client=s3_des_client,
                bucket=des_bucket,
                S3Prefix=des_prefix,
                table=table,
                JobsenderCompareVersionId=JobsenderCompareVersionId
            )
            # Generate job list
            job_list, ignore_records = delta_job_list(
                src_file_list=src_file_list,
                des_file_list=des_file_list,
                src_bucket=src_bucket,
                src_prefix=src_prefix,
                des_bucket=des_bucket,
                des_prefix=des_prefix,
                ignore_list=ignore_list,
                JobsenderCompareVersionId=JobsenderCompareVersionId
            )

            # Upload jobs to sqs
            if len(job_list) != 0:
                job_upload_sqs_ddb(
                    sqs=sqs,
                    sqs_queue=sqs_queue,
                    job_list=job_list
                )
                max_object = max(job_list, key=itemgetter('Size'))
                MaxChunkSize = int(max_object['Size'] / 10000) + 1024
                if MaxChunkSize < 5*1024*1024:
                    MaxChunkSize = 5*1024*1024
                logger.warning(f'Max object size in job_list: {max_object["Size"]}.\n Require instance memory'
                               f' > MaxChunksize x MaxThread x MaxParallelFile, i.e. '
                               f'{MaxChunkSize} x {MaxThread} x {MaxParallelFile} = '
                               f'{MaxChunkSize*MaxThread*MaxParallelFile}.\n If less memory, instance may crash!')
            else:
                logger.info('Source list are all in Destination, no job to send.')

            # Just backup for debug
            logger.info('Writing job and ignore list to local file backup...')
            t = time.localtime()
            start_time = f'{t.tm_year}-{t.tm_mon}-{t.tm_mday}-{t.tm_hour}-{t.tm_min}-{t.tm_sec}'
            log_path = str(Path(log_file_name).parent)
            if job_list:
                local_backup_list = f'{log_path}/job-list-{src_bucket}-{start_time}.json'
                with open(local_backup_list, 'w') as f:
                    json.dump(job_list, f)
                logger.info(f'Write Job List: {os.path.abspath(local_backup_list)}')
            if ignore_records:
                local_ignore_records = f'{log_path}/ignore-records-{src_bucket}-{start_time}.json'
                with open(local_ignore_records, 'w') as f:
                    json.dump(ignore_records, f)
                logger.info(f'Write Ignore List: {os.path.abspath(local_ignore_records)}')

    else:
        logger.error('Job sqs queue is not empty or fail to get_queue_attributes. Stop process.')
    print('Completed and logged to file:', os.path.abspath(log_file_name))
