# The poll ticker. One curl against the LangGraph deployment, nothing else.
#
# Built rather than using curlimages/curl directly so the Devtron app is an
# ordinary git -> CI -> CD flow with an image you control, instead of a chart
# pointed at a third-party tag that could move under you.
FROM curlimages/curl:8.11.1

COPY poll.sh /usr/local/bin/poll.sh

USER 65532:65532
ENTRYPOINT ["/bin/sh", "/usr/local/bin/poll.sh"]
