#pragma once

#include <QMetaType>
#include <QVideoFrame>

extern "C" {
#include <libavutil/rational.h>
}

struct MediaStreamInfo
{
    int videoStreamIndex = -1;

    AVRational videoTimeBase{1, 1};

    int width = 0;
    int height = 0;
    bool stillImage = false;
};

// Decoded frame: QVideoFrame wraps the QImage via implicit sharing.
// The decoder re uses a fixed pool of QImage buffers; a frame must be released by the renderer before that slot can be reused.
struct DecodedVideoFrame
{
    QVideoFrame frame;
    double      ptsSeconds = 0.0;
};

Q_DECLARE_METATYPE(DecodedVideoFrame)
