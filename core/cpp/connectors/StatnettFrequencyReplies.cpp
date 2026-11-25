#include "StatnettClient.hpp"
#include "StatnettReplyUtils.hpp"

#include <QDateTime>
#include <QDebug>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonValue>
#include <QList>
#include <QNetworkRequest>
#include <QPair>
#include <QRegularExpression>
#include <QTimeZone>
#include <QXmlStreamReader>
#include <cmath>

using StatnettReplyUtils::utcNow;
using StatnettReplyUtils::elementText;
using StatnettReplyUtils::parseNumber;
using StatnettReplyUtils::jsonDateTime;

namespace {
bool validGridFrequency(double value)
{
    return std::isfinite(value) && value >= 45.0 && value <= 55.0;
}

bool parseFrequencyJson(const QByteArray& data, double* frequency, QString* timestampUtc)
{
    const QJsonDocument document = QJsonDocument::fromJson(data);
    if (document.isNull())
        return false;

    const QDateTime now = QDateTime::currentDateTimeUtc().addSecs(30);
    QDateTime selectedTime;
    double selectedValue = 0.0;
    bool found = false;
    if (document.isObject()) {
        const QJsonObject root = document.object();
        const QDateTime start = jsonDateTime(root.value(QStringLiteral("StartPointUTC")));
        bool tickOk = false;
        double tickMs = root.value(QStringLiteral("PeriodTickMs")).toVariant().toDouble(&tickOk);
        // ByMinute has a one-minute cadence; tolerate APIs serializing the
        // interval as text or omitting the redundant sampling interval.
        if (!tickOk || !std::isfinite(tickMs) || tickMs <= 0.0)
            tickMs = 60000.0;
        const QJsonArray measurements = root.value(QStringLiteral("Measurements")).toArray();
        if (start.isValid() && !measurements.isEmpty()) {
            for (qsizetype index = 0; index < measurements.size(); ++index) {
                const QJsonValue item = measurements.at(index);
                double sample = 0.0;
                if (item.isDouble()) {
                    sample = item.toDouble();
                } else if (item.isObject()) {
                    parseNumber(elementText(item.toObject(), {"Value", "Frequency", "frequency", "value"}), &sample);
                } else {
                    parseNumber(item.toString(), &sample);
                }
                const QDateTime sampleTime = start.addMSecs(static_cast<qint64>(std::llround(index * tickMs)));
                if (validGridFrequency(sample) && sampleTime <= now && (!found || sampleTime > selectedTime)) {
                    selectedValue = sample;
                    selectedTime = sampleTime;
                    found = true;
                }
            }
        }

        // Some deployments return a single latest reading instead of the
        // chart-series envelope. Retain this as a compatible fallback.
        if (!found) {
            double sample = 0.0;
            const QString valueText = elementText(root, {"Value", "Frequency", "frequency", "value"});
            if (parseNumber(valueText, &sample) && validGridFrequency(sample)) {
                selectedValue = sample;
                selectedTime = jsonDateTime(root.value(QStringLiteral("TimestampUTC")));
                if (!selectedTime.isValid())
                    selectedTime = QDateTime::currentDateTimeUtc();
                found = true;
            }
        }
    } else if (document.isArray()) {
        const QJsonArray records = document.array();
        for (const QJsonValue& value : records) {
            if (!value.isObject())
                continue;
            const QJsonObject row = value.toObject();
            double sample = 0.0;
            if (!parseNumber(elementText(row, {"Value", "Frequency", "frequency", "value"}), &sample)
                || !validGridFrequency(sample))
                continue;
            QDateTime sampleTime = jsonDateTime(row.value(QStringLiteral("TimestampUTC")));
            if (!sampleTime.isValid())
                sampleTime = jsonDateTime(row.value(QStringLiteral("timestamp")));
            if (!sampleTime.isValid())
                sampleTime = QDateTime::currentDateTimeUtc();
            if (sampleTime <= now && (!found || sampleTime > selectedTime)) {
                selectedValue = sample;
                selectedTime = sampleTime;
                found = true;
            }
        }
    }
    if (!found)
        return false;
    *frequency = selectedValue;
    if (timestampUtc)
        *timestampUtc = selectedTime.toString(Qt::ISODateWithMs);
    return true;
}

bool parseFrequencyXml(const QByteArray& data, double* frequency, QString* timestampUtc)
{
    QXmlStreamReader xml(data);
    QDateTime startTime;
    QDateTime endTime;
    double tickMs = 0.0;
    QList<QPair<qsizetype, double>> measurements;
    qsizetype sampleIndex = 0;
    while (!xml.atEnd()) {
        xml.readNext();
        if (!xml.isStartElement())
            continue;

        const QString name = xml.name().toString();
        if (name.compare(QStringLiteral("Measurements"), Qt::CaseInsensitive) == 0) {
            const QString valuesText = xml.readElementText(QXmlStreamReader::IncludeChildElements).trimmed();
            static const QRegularExpression numberPattern(
                QStringLiteral(R"([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)"));
            QRegularExpressionMatchIterator matches = numberPattern.globalMatch(valuesText);
            while (matches.hasNext()) {
                double sample = 0.0;
                if (parseNumber(matches.next().captured(), &sample) && validGridFrequency(sample))
                    measurements.append(qMakePair(sampleIndex, sample));
                ++sampleIndex;
            }
        } else if (name.compare(QStringLiteral("StartPointUTC"), Qt::CaseInsensitive) == 0) {
            startTime = jsonDateTime(QJsonValue(xml.readElementText().trimmed()));
        } else if (name.compare(QStringLiteral("EndPointUTC"), Qt::CaseInsensitive) == 0) {
            endTime = jsonDateTime(QJsonValue(xml.readElementText().trimmed()));
        } else if (name.compare(QStringLiteral("PeriodTickMs"), Qt::CaseInsensitive) == 0) {
            parseNumber(xml.readElementText().trimmed(), &tickMs);
        }
        // Ignore wrapper elements and continue traversing their children.
    }
    if (xml.hasError() || measurements.isEmpty())
        return false;

    const qint64 spacing = tickMs > 0.0 ? static_cast<qint64>(std::llround(tickMs)) : 60000;
    const QDateTime now = QDateTime::currentDateTimeUtc().addSecs(30);
    bool found = false;
    QDateTime selectedTime;
    double selectedValue = 0.0;
    for (const auto& measurement : measurements) {
        const QDateTime sampleTime = startTime.isValid()
            ? startTime.addMSecs(spacing * measurement.first)
            : endTime.isValid() ? endTime : QDateTime::currentDateTimeUtc();
        if (sampleTime <= now && (!found || sampleTime > selectedTime)) {
            selectedTime = sampleTime;
            selectedValue = measurement.second;
            found = true;
        }
    }
    if (!found || !validGridFrequency(selectedValue))
        return false;
    *frequency = selectedValue;
    if (timestampUtc)
        *timestampUtc = selectedTime.toUTC().toString(Qt::ISODateWithMs);
    return true;
}
}

void StatnettClient::onFrequencyReply(QNetworkReply* reply)
{
    const int httpStatus = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
    const QString contentType = reply->header(QNetworkRequest::ContentTypeHeader).toString();
    if (reply->error() != QNetworkReply::NoError) {
        qWarning().noquote() << "[feed:frequency] request_failed"
                             << "url=" << reply->request().url().toString(QUrl::RemoveQuery)
                             << "http_status=" << httpStatus
                             << "content_type=" << contentType
                             << "error=" << reply->errorString();
        reportFailure(reply, QStringLiteral("Statnett frequency"));
        return;
    }
    const QByteArray data = reply->readAll();
    emit rawDataReceived(QStringLiteral("Statnett Frequency/ByMinute"), data);
    QString payloadShape;
    const QByteArray trimmed = data.trimmed();
    if (trimmed.startsWith('{')) {
        const QJsonDocument document = QJsonDocument::fromJson(trimmed);
        if (document.isObject()) {
            const QStringList keys = document.object().keys();
            payloadShape = QStringLiteral("object keys=[%1]").arg(keys.join(QStringLiteral(", ")));
        } else {
            payloadShape = QStringLiteral("invalid JSON object");
        }
    } else if (trimmed.startsWith('[')) {
        const QJsonDocument document = QJsonDocument::fromJson(trimmed);
        payloadShape = QStringLiteral("array size=%1").arg(document.isArray() ? document.array().size() : 0);
    } else if (trimmed.startsWith('<')) {
        payloadShape = QStringLiteral("XML bytes=%1").arg(trimmed.size());
    } else {
        payloadShape = QStringLiteral("unexpected text prefix=%1").arg(QString::fromUtf8(trimmed.left(32)));
    }
    double frequency = 0.0;
    bool found = false;
    QString frequencyTimestamp;
    if (trimmed.startsWith('<'))
        found = parseFrequencyXml(trimmed, &frequency, &frequencyTimestamp);
    else
        found = parseFrequencyJson(trimmed, &frequency, &frequencyTimestamp);
    qDebug().noquote() << "[feed:frequency] response"
                       << "url=" << reply->request().url().toString(QUrl::RemoveQuery)
                       << "http_status=" << httpStatus
                       << "content_type=" << contentType
                       << "bytes=" << trimmed.size()
                       << "shape=" << payloadShape;
    if (!found) {
        m_latestTelemetry.hasFrequency = false;
        m_latestTelemetry.frequencyHz = 0.0;
        m_latestTelemetry.sourceStatus = QStringLiteral("Statnett Frequency/ByMinute returned no valid 45-55 Hz sample (bytes=%1, http=%2)")
            .arg(trimmed.size()).arg(httpStatus);
        qWarning().noquote() << "[feed:frequency] no valid ByMinute sample"
                             << "http_status=" << httpStatus
                             << "content_type=" << contentType
                             << "bytes=" << trimmed.size()
                             << "shape=" << payloadShape;
    } else {
        m_latestTelemetry.frequencyHz = frequency;
        m_latestTelemetry.hasFrequency = true;
        m_latestTelemetry.lastUpdatedUtc = frequencyTimestamp.isEmpty() ? utcNow() : frequencyTimestamp;
        m_latestTelemetry.sourceStatus = QStringLiteral("Frequency/ByMinute sample received from Statnett (%1 Hz)").arg(frequency, 0, 'f', 3);
        qDebug().noquote() << "[feed:frequency] parsed frequency_hz=" << frequency
                           << "timestamp_utc=" << m_latestTelemetry.lastUpdatedUtc;
    }
    m_latestTelemetry.frequencyUpdated = true;
    emit telemetryReceived(m_latestTelemetry);
    m_latestTelemetry.frequencyUpdated = false;
    reply->deleteLater();
}
