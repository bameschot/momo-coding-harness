#!/usr/bin/env bash
build() {
  make all
}
function deploy {
  build && echo done
}
deploy
