#pragma once

#include "telemetry/BatTelemetry.hpp"

#include <QByteArray>
#include <QString>

class TelemetryPacketParser
{
public:

    static constexpr qsizetype
        MinimumHeaderSize = 40;

    static bool parse(
        const QByteArray& datagram,
        BatTelemetry& output,
        QString* error = nullptr
    );

private:

    static bool readU16(
        const QByteArray& data,
        qsizetype offset,
        quint16& value
    );

    static bool readU32(
        const QByteArray& data,
        qsizetype offset,
        quint32& value
    );

    static bool readU64(
        const QByteArray& data,
        qsizetype offset,
        quint64& value
    );

    static bool readI32(
        const QByteArray& data,
        qsizetype offset,
        qint32& value
    );
};