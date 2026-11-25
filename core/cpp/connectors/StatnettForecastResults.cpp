#include "StatnettClient.hpp"

#include <QDateTime>
#include <QDebug>
#include <QJsonArray>
#include <QJsonValue>
#include <QMap>
#include <QSet>
#include <QVariantMap>
#include <cmath>
#include <limits>

namespace {
QString utcNow()
{
    return QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs);
}

struct ParsedForecastSeries {
    bool valid = true;
    QString error;
    QVariantList records;
    QSet<QString> timestamps;
    QMap<QString, double> firstPrices;
    QString firstTimestamp;
};

ParsedForecastSeries parseForecastSeries(const QJsonArray& input, const QString& label)
{
    ParsedForecastSeries parsed;
    if (input.isEmpty())
        return parsed;

    const QSet<QString> expectedAreas{
        QStringLiteral("NO1"), QStringLiteral("NO2"), QStringLiteral("NO3"),
        QStringLiteral("NO4"), QStringLiteral("NO5")
    };
    QMap<QString, QSet<QString>> areasByTimestamp;
    for (const QJsonValue& value : input) {
        if (!value.isObject()) {
            parsed.valid = false;
            parsed.error = label + QStringLiteral(" contains a non-object price record.");
            return parsed;
        }
        const QJsonObject record = value.toObject();
        const QString area = record.value(QStringLiteral("area")).toString();
        const QString timestamp = record.value(QStringLiteral("timestamp")).toString();
        const double price = record.value(QStringLiteral("price_eur_mwh")).toDouble(
            std::numeric_limits<double>::quiet_NaN());
        if (!expectedAreas.contains(area) || timestamp.isEmpty() || !std::isfinite(price)) {
            parsed.valid = false;
            parsed.error = label + QStringLiteral(" contains an invalid area, timestamp or price.");
            return parsed;
        }

        QDateTime timestampValue = QDateTime::fromString(timestamp, Qt::ISODateWithMs);
        if (!timestampValue.isValid())
            timestampValue = QDateTime::fromString(timestamp, Qt::ISODate);
        if (!timestampValue.isValid() || timestampValue.timeSpec() == Qt::LocalTime) {
            parsed.valid = false;
            parsed.error = label + QStringLiteral(" timestamps must include an explicit timezone.");
            return parsed;
        }
        const QDateTime timestampUtc = timestampValue.toUTC();
        const QString timestampKey = timestampUtc.toString(Qt::ISODateWithMs);
        if (areasByTimestamp[timestampKey].contains(area)) {
            parsed.valid = false;
            parsed.error = label + QStringLiteral(" contains duplicate area/MTU records.");
            return parsed;
        }
        areasByTimestamp[timestampKey].insert(area);
        parsed.timestamps.insert(timestampKey);
        if (parsed.firstTimestamp.isEmpty() || timestampUtc < QDateTime::fromString(parsed.firstTimestamp, Qt::ISODateWithMs))
            parsed.firstTimestamp = timestampKey;

        QVariantMap normalized = record.toVariantMap();
        normalized.insert(QStringLiteral("timestamp_utc"), timestampKey);
        normalized.insert(QStringLiteral("timestamp_ms"), timestampUtc.toMSecsSinceEpoch());
        parsed.records.append(normalized);
        parsed.firstPrices.insert(area, price);
    }

    for (auto it = areasByTimestamp.cbegin(); it != areasByTimestamp.cend(); ++it) {
        if (it.value() != expectedAreas) {
            parsed.valid = false;
            parsed.error = label + QStringLiteral(" must contain exactly five unique Norwegian zone prices per MTU.");
            return parsed;
        }
    }
    parsed.firstPrices.clear();
    for (const QJsonValue& value : input) {
        const QJsonObject record = value.toObject();
        QDateTime timestampValue = QDateTime::fromString(record.value(QStringLiteral("timestamp")).toString(), Qt::ISODateWithMs);
        if (!timestampValue.isValid())
            timestampValue = QDateTime::fromString(record.value(QStringLiteral("timestamp")).toString(), Qt::ISODate);
        if (timestampValue.toUTC().toString(Qt::ISODateWithMs) == parsed.firstTimestamp)
            parsed.firstPrices.insert(record.value(QStringLiteral("area")).toString(),
                                      record.value(QStringLiteral("price_eur_mwh")).toDouble());
    }
    return parsed;
}
}

void StatnettClient::acceptDayAheadForecastResult(const QJsonObject& result, const QString& scenarioId)
{
    const QJsonObject modelRecords = result.value(QStringLiteral("model_price_records")).toObject();
    const QJsonArray recordsA = modelRecords.value(QStringLiteral("A")).toArray();
    const QJsonArray recordsB = modelRecords.value(QStringLiteral("B")).toArray();
    if (result.value(QStringLiteral("interval_count")).toInt() < 1 || (recordsA.isEmpty() && recordsB.isEmpty())) {
        emit fetchError(QStringLiteral("Day-ahead %1 calculation returned no modelled price records.")
            .arg(m_currentDayAheadMode.toLower()));
        return;
    }

    const ParsedForecastSeries seriesA = parseForecastSeries(recordsA, QStringLiteral("Approach A"));
    const ParsedForecastSeries seriesB = parseForecastSeries(recordsB, QStringLiteral("Approach B"));
    if (!seriesA.valid || !seriesB.valid) {
        emit fetchError(!seriesA.valid ? seriesA.error : seriesB.error);
        return;
    }

    const bool useBFirst = !recordsB.isEmpty()
        && (recordsA.isEmpty() || seriesB.firstTimestamp <= seriesA.firstTimestamp);
    const QMap<QString, double>& firstPrices = useBFirst ? seriesB.firstPrices : seriesA.firstPrices;
    const QString firstTimestamp = useBFirst ? seriesB.firstTimestamp : seriesA.firstTimestamp;
    if (firstPrices.size() != 5 || firstTimestamp.isEmpty()) {
        emit fetchError(QStringLiteral("The earliest modelled MTU does not contain all five Norwegian area prices."));
        return;
    }

    m_latestTelemetry.modPriceNO1 = firstPrices.value(QStringLiteral("NO1"));
    m_latestTelemetry.modPriceNO2 = firstPrices.value(QStringLiteral("NO2"));
    m_latestTelemetry.modPriceNO3 = firstPrices.value(QStringLiteral("NO3"));
    m_latestTelemetry.modPriceNO4 = firstPrices.value(QStringLiteral("NO4"));
    m_latestTelemetry.modPriceNO5 = firstPrices.value(QStringLiteral("NO5"));
    m_latestTelemetry.hasModelledPrices = true;
    m_latestTelemetry.modelledDeliveryTimeUtc = firstTimestamp;
    m_latestTelemetry.modelledDeliveryDayLocal = result.value(QStringLiteral("delivery_day_local")).toString();
    m_latestTelemetry.modelledForecastOriginUtc = result.value(QStringLiteral("forecast_origin_utc")).toString();
    m_latestTelemetry.modelledPriceSeriesA = seriesA.records;
    m_latestTelemetry.modelledPriceSeriesB = seriesB.records;
    m_latestTelemetry.activeScenario = scenarioId;

    const int receivedIntervals = qMax(seriesA.timestamps.size(), seriesB.timestamps.size());
    const int readyIntervals = result.value(QStringLiteral("ready_interval_count")).toInt(receivedIntervals);
    const int missingIntervals = result.value(QStringLiteral("coverage")).toObject()
        .value(QStringLiteral("missing_interval_count")).toInt();
    const QJsonObject bModel = result.value(QStringLiteral("models")).toObject()
        .value(QStringLiteral("B")).toObject();
    const QJsonArray bIntervals = bModel.value(QStringLiteral("intervals")).toArray();
    QString bindingText = QStringLiteral("No positive-dual JAO CNEC was reported in the solved B intervals.");
    double largestShadowPrice = 0.0;
    for (const QJsonValue& intervalValue : bIntervals) {
        const QJsonObject interval = intervalValue.toObject();
        const QJsonObject cnecSummary = interval.value(QStringLiteral("cnec_diagnostics")).toObject();
        const QJsonArray activeCnecs = cnecSummary.value(QStringLiteral("binding_cnecs")).toArray();
        for (const QJsonValue& cnecValue : activeCnecs) {
            const QJsonObject cnec = cnecValue.toObject();
            const double shadowPrice = cnec.value(QStringLiteral("shadow_price_eur_per_mw")).toDouble();
            if (std::isfinite(shadowPrice) && shadowPrice > largestShadowPrice) {
                largestShadowPrice = shadowPrice;
                const QString identifier = cnec.value(QStringLiteral("constraint_id")).toString();
                const QString name = cnec.value(QStringLiteral("cnec_name")).toString();
                const QString label = name.isEmpty() ? identifier : name;
                bindingText = QStringLiteral("JAO CNEC %1 · shadow value %2 EUR/MWh · %3")
                    .arg(label).arg(shadowPrice, 0, 'f', 2)
                    .arg(interval.value(QStringLiteral("delivery_start_utc")).toString());
            }
        }
    }
    const QString hydroMode = bModel.value(QStringLiteral("hydro_coupling_mode")).toString();
    m_latestTelemetry.modelBasisExplanation = QStringLiteral(
        "Approach B is the primary multi-period LP; A fills gaps. Hydro coupling: %1.")
        .arg(hydroMode.isEmpty() ? QStringLiteral("not reported") : hydroMode);
    m_latestTelemetry.bindingConstraint = bindingText;
    m_latestTelemetry.sourceStatus = QStringLiteral("Day-ahead forecast loaded: %1 received MTUs, %2 ready, %3 missing; A=%4 price records, B=%5 price records")
        .arg(receivedIntervals).arg(readyIntervals).arg(missingIntervals)
        .arg(seriesA.records.size()).arg(seriesB.records.size());
    m_latestTelemetry.lastUpdatedUtc = utcNow();
    emit telemetryReceived(m_latestTelemetry);
    qDebug().noquote() << "[cvxpy:day-ahead-prices] delivery_day=" << m_latestTelemetry.modelledDeliveryDayLocal
                       << "approach_A_records=" << seriesA.records.size()
                       << "approach_B_records=" << seriesB.records.size()
                       << "first_MTU_NO1-NO5=" << firstPrices;
}
