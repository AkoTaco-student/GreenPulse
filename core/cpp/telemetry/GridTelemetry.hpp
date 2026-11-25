#pragma once

#include <cstdint>
#include <QVariantList>
#include <QString>

// Normalized values with explicit provenance; zero is never treated as a measurement
// unless the corresponding has* flag is true.
struct GridTelemetry
{
    uint64_t timestampUs = 0;
    uint32_t sequence = 0;

    double frequencyHz = 0.0;
    bool hasFrequency = false;
    bool frequencyUpdated = false;

    QString dataSourceMode = QStringLiteral("OBSERVED");
    QString dayAheadMode = QStringLiteral("REPLAY"); // Calculation mode; replay is the safe startup default.
    QString activeScenario = QStringLiteral("normal_base");
    QString sourceStatus = QStringLiteral("Venter på datakilder");
    QString lastUpdatedUtc;
    QString bindingConstraint = QStringLiteral("Ingen beregnet nettstatus ennå.");
    QString modelBasisExplanation = QStringLiteral("Venter på modellresultat og observerte priser.");

    double priceNO1 = 0.0;
    double priceNO2 = 0.0;
    double priceNO3 = 0.0;
    double priceNO4 = 0.0;
    double priceNO5 = 0.0;
    double systemPrice = 0.0;
    bool hasObservedPrices = false;
    bool hasObservedPriceNO1 = false;
    bool hasObservedPriceNO2 = false;
    bool hasObservedPriceNO3 = false;
    bool hasObservedPriceNO4 = false;
    bool hasObservedPriceNO5 = false;
    QVariantList observedPriceSeries;
    QString observedPriceDayStartUtc;
    QString observedPriceDayEndUtc;

    double obsPriceNO1 = 0.0;
    double obsPriceNO2 = 0.0;
    double obsPriceNO3 = 0.0;
    double obsPriceNO4 = 0.0;
    double obsPriceNO5 = 0.0;

    double modPriceNO1 = 0.0;
    double modPriceNO2 = 0.0;
    double modPriceNO3 = 0.0;
    double modPriceNO4 = 0.0;
    double modPriceNO5 = 0.0;
    bool hasModelledPrices = false;
    QString modelledDeliveryTimeUtc;
    QString modelledDeliveryDayLocal;
    QString modelledForecastOriginUtc;
    QVariantList modelledPriceSeriesA;
    QVariantList modelledPriceSeriesB;

    // Modelled DC-dispatch line results (not measured physical line flows).
    double flowNO1_NO2 = 0.0;
    double flowNO1_NO3 = 0.0;
    double flowNO1_NO5 = 0.0;
    double flowNO2_NO5 = 0.0;
    double flowNO3_NO4 = 0.0;
    double flowNO3_NO5 = 0.0;
    double capNO1_NO2 = 0.0;
    double capNO1_NO3 = 0.0;
    double capNO1_NO5 = 0.0;
    double capNO2_NO5 = 0.0;
    double capNO3_NO4 = 0.0;
    double capNO3_NO5 = 0.0;
    double shadowPriceNO1_NO3 = 0.0;
    double shadowPriceNO3_NO4 = 0.0;

    // Raw normalized Statnett PhysicalFlowMap records (XML source).
    QVariantList physicalFlows;
    bool hasPhysicalFlows = false;
    int physicalFlowCount = 0;

    double netExportEuropeMW = 0.0;
    bool hasNetExportEurope = false;
    double mfrrActivatedMW = 0.0;
    uint8_t qualityStatus = 2; // 0=OK, 1=partial/interpolated, 2=missing/stale
};
