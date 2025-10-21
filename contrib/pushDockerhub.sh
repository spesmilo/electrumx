#!/bin/bash

CONTAINER_NAME=knfcoin-electrumx

TODAY=$(date +%Y%m%d)
sudo docker login -u admin@knf.vu.lt

# Build from parent directory context, using this Dockerfile
sudo docker build -f Dockerfile -t $CONTAINER_NAME ..

sudo docker tag $CONTAINER_NAME vuknf/$CONTAINER_NAME:latest
sudo docker tag $CONTAINER_NAME vuknf/$CONTAINER_NAME:$TODAY
sudo docker push vuknf/$CONTAINER_NAME:latest
sudo docker push vuknf/$CONTAINER_NAME:$TODAY