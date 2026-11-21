"""Development-only synthetic condition profile isolation and immutability check."""
import os
from uuid import uuid4

import boto3
from boto3.dynamodb.conditions import Key
from foodtech_business_api.cooking_store import Conflict, DynamoCookingStore

TABLE, REGION, ACCOUNT = "foodtech-cooking-jobs-dev", "ap-southeast-2", "573693340100"


def main():
    if os.environ.get("AWS_PROFILE") != "foodtech":
        raise SystemExit("Use the foodtech development profile")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account")
    store = DynamoCookingStore(TABLE)
    subject = f"synthetic-profile-{uuid4().hex}"
    profile = {"profile_id": "synthetic-small", "profile_version": "v1", "equipment_profile": "jeyuk-small",
               "heating_setpoint_celsius": "200", "target_rpm": "30", "provenance": "SYNTHETIC",
               "status": "DECLARED_UNVERIFIED"}
    pk = f"SCOPE#USER#{subject}#CONDITION_PROFILE#{profile['profile_id']}"
    try:
        store.create_condition_profile(subject, profile)
        try:
            store.create_condition_profile(subject, {**profile, "target_rpm": "31"})
        except Conflict:
            pass
        else:
            raise AssertionError("existing profile version was overwritten")
        if store.condition_profile(subject, profile["profile_id"], "v1")["target_rpm"] != "30" or \
                store.condition_profile(subject + "-other", profile["profile_id"], "v1") is not None:
            raise AssertionError("profile immutability or customer scope failed")
        print("Synthetic profile immutability and customer scope verified")
    finally:
        store.table.delete_item(Key={"pk": pk, "sk": "VERSION#v1"})
        if store.table.query(KeyConditionExpression=Key("pk").eq(pk), ConsistentRead=True)["Items"]:
            raise RuntimeError("synthetic profile cleanup failed")
        print("Synthetic profile cleanup verified")


if __name__ == "__main__": main()
