#!/bin/bash

curl localhost:8080/wm/staticentrypusher/list/all/json | python -m json.tool

