#!/bin/bash

if [ "$TRAVIS_BRANCH" == "master" ];
then
    cd flask/docker;
    docker login -u "$DOCKER_USERNAME" -p "$DOCKER_PASSWORD";
    docker build -t michael13162/sbolexplorer:snapshot -f Dockerfile .;
    docker build -t michael13162/sbolexplorer:snapshot-synbiohub -f Dockerfile-synbiohub .;
    docker push michael13162/sbolexplorer:snapshot;
    docker push michael13162/sbolexplorer:snapshot-synbiohub;
fi
