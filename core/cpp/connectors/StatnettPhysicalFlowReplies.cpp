#include "StatnettClient.hpp"
#include "StatnettReplyUtils.hpp"

#include <QDebug>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QMap>
#include <QStringList>
#include <QVariantMap>
#include <QXmlStreamReader>

using StatnettReplyUtils::utcNow;
using StatnettReplyUtils::parseNumber;

namespace {
QVariantList parsePhysicalFlowXml(const QByteArray& data, QString* error)
{
    QVariantList records;
    QXmlStreamReader xml(data);
    QVariantMap item;
    bool inItem = false;
    while (!xml.atEnd()) {
        xml.readNext();
        if (xml.isStartElement()) {
            const QString name = xml.name().toString();
            if (name == QStringLiteral("PhysicalFlowMapFlowItem")) {
                item.clear();
                inItem = true;
                continue;
            }
            if (inItem) {
                const QString value = xml.readElementText().trimmed();
                if (name == QStringLiteral("InAreaElspotId"))
                    item.insert(QStringLiteral("in_area"), value);
                else if (name == QStringLiteral("OutAreaElspotId"))
                    item.insert(QStringLiteral("out_area"), value);
                else if (name == QStringLiteral("MeasureDate")) {
                    bool ok = false;
                    const qint64 epochMs = value.toLongLong(&ok);
                    item.insert(QStringLiteral("timestamp_utc"), ok
                        ? QDateTime::fromMSecsSinceEpoch(epochMs, Qt::UTC).toString(Qt::ISODateWithMs)
                        : value);
                } else if (name == QStringLiteral("Value")) {
                    double flow = 0.0;
                    if (parseNumber(value, &flow))
                        item.insert(QStringLiteral("value_mw"), flow);
                }
            }
        } else if (xml.isEndElement() && xml.name() == QStringLiteral("PhysicalFlowMapFlowItem")) {
            if (!item.isEmpty())
                records.append(item);
            item.clear();
            inItem = false;
        }
    }
    if (xml.hasError() && error)
        *error = xml.errorString();
    return records;
}
}

void StatnettClient::onFlowsReply(QNetworkReply* reply)
{
    if (reply->error() != QNetworkReply::NoError) {
        reportFailure(reply, QStringLiteral("Statnett physical flow"));
        return;
    }
    const QByteArray data = reply->readAll();
    /*qDebug().noquote() << "[feed:physical-flow] response http="
                       << reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt()
                       << "bytes=" << data.size()
                       << "content-type=" << reply->header(QNetworkRequest::ContentTypeHeader).toString();*/
    emit rawDataReceived(QStringLiteral("Statnett physical flows"), data);
    QString parseError;
    QVariantList records;
    if (data.trimmed().startsWith('<')) {
        //qDebug() << "[feed:physical-flow] parsing XML";
        records = parsePhysicalFlowXml(data, &parseError);
    } else {
        //qDebug() << "[feed:physical-flow] parsing JSON";
        const QJsonDocument document = QJsonDocument::fromJson(data);
        const QJsonValue root = document.isArray() ? QJsonValue(document.array())
            : document.isObject() ? QJsonValue(document.object()) : QJsonValue();
        if (root.isArray()) {
            for (const QJsonValue& item : root.toArray())
                if (item.isObject()) records.append(item.toObject().toVariantMap());
        } else if (root.isObject()) {
            const QJsonObject object = root.toObject();
            const QJsonArray rows = object.value(QStringLiteral("Flows")).toArray();
            for (const QJsonValue& item : rows)
                if (item.isObject()) records.append(item.toObject().toVariantMap());
        } else {
            parseError = QStringLiteral("Ugyldig JSON-respons");
        }
    }
    if (records.isEmpty()) {
        //qWarning().noquote() << "[feed:physical-flow] parsed zero records; parseError=" << parseError;
        emit fetchError(QStringLiteral("Statnett flytdata kunne ikke normaliseres: %1").arg(parseError));
    } else {
        QStringList sample;
        QMap<QString, QStringList> norwegianCorridors;
        for (const QVariant& recordValue : records) {
            const QVariantMap record = recordValue.toMap();
            const QString fromArea = record.value(QStringLiteral("in_area"),
                record.value(QStringLiteral("InAreaElspotId"))).toString();
            const QString toArea = record.value(QStringLiteral("out_area"),
                record.value(QStringLiteral("OutAreaElspotId"))).toString();
            const double flowMw = record.value(QStringLiteral("value_mw"),
                record.value(QStringLiteral("Value"))).toDouble();
            const QString timestamp = record.value(QStringLiteral("timestamp_utc"),
                record.value(QStringLiteral("MeasureDate"))).toString();
            if (fromArea.startsWith(QStringLiteral("NO")) && toArea.startsWith(QStringLiteral("NO"))) {
                const QString corridor = fromArea < toArea
                    ? fromArea + QStringLiteral("-") + toArea
                    : toArea + QStringLiteral("-") + fromArea;
                norwegianCorridors[corridor].append(
                    QStringLiteral("%1->%2 %3 MW @ %4")
                        .arg(fromArea, toArea)
                        .arg(flowMw, 0, 'f', 1)
                        .arg(timestamp));
            }
            if (sample.size() < 3) {
                sample.append(QStringLiteral("%1->%2 %3 MW @ %4")
                    .arg(fromArea, toArea)
                    .arg(flowMw, 0, 'f', 1)
                    .arg(timestamp));
            }
        }
        /*qDebug().noquote() << "[feed:physical-flow] normalized_records=" << records.size()
                           << "internal_NO_corridor_measurements=" << norwegianCorridors
                           << "first_record_fields=" << records.first().toMap().keys()
                           << "sample=" << sample.join(QStringLiteral(" | "));*/
        m_latestTelemetry.physicalFlows = records;
        m_latestTelemetry.physicalFlowCount = records.size();
        m_latestTelemetry.hasPhysicalFlows = true;
        m_latestTelemetry.lastUpdatedUtc = utcNow();
        m_latestTelemetry.sourceStatus = QStringLiteral("%1 Statnett-fysiske flytobservasjoner mottatt")
            .arg(records.size());
        emit telemetryReceived(m_latestTelemetry);
    }
    reply->deleteLater();
}
