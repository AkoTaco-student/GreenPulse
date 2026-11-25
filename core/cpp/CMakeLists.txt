cmake_minimum_required(VERSION 3.16)

project(greenpulse_core LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(PkgConfig REQUIRED)

# Allow a local FFmpeg SDK without baking a developer-specific absolute path
# into the project. Set FFMPEG_ROOT when using a non-standard installation.
set(greenpulse_FFMPEG_ROOT "$ENV{FFMPEG_ROOT}" CACHE PATH "FFmpeg SDK prefix")
if(NOT greenpulse_FFMPEG_ROOT AND EXISTS "$ENV{HOME}/ffmpeg-lgpl/install")
    set(greenpulse_FFMPEG_ROOT "$ENV{HOME}/ffmpeg-lgpl/install")
endif()
if(greenpulse_FFMPEG_ROOT AND EXISTS "${greenpulse_FFMPEG_ROOT}/lib/pkgconfig")
    set(ENV{PKG_CONFIG_PATH} "${greenpulse_FFMPEG_ROOT}/lib/pkgconfig:$ENV{PKG_CONFIG_PATH}")
endif()

pkg_check_modules(FFMPEG REQUIRED IMPORTED_TARGET
    libavformat 
    libavcodec 
    libavutil 
    libswscale
)

find_package(Qt6 REQUIRED COMPONENTS 
    Core 
    Gui 
    Qml 
    Quick 
    Multimedia
    Network
)

qt_standard_project_setup()

set(greenpulse_SOURCES
    monitor/h/media/Demuxer.hpp
    monitor/h/media/MediaSession.hpp
    monitor/h/media/MediaTypes.hpp
    monitor/h/media/PacketQueue.hpp
    monitor/h/media/VideoDecoder.hpp
    monitor/h/media/VideoRenderer.hpp
    monitor/h/ShopPlayer.hpp

    telemetry/GridTelemetry.hpp
    telemetry/MetadataStore.hpp

    connectors/StatnettClient.hpp

    main.cpp
    monitor/src/ShopPlayer.cpp

    monitor/src/media/Demuxer.cpp
    monitor/src/media/MediaSession.cpp
    monitor/src/media/PacketQueue.cpp
    monitor/src/media/VideoDecoder.cpp
    monitor/src/media/VideoRenderer.cpp

    telemetry/MetadataStore.cpp
    connectors/StatnettClient.cpp
    connectors/StatnettPriceReplies.cpp
    connectors/StatnettPhysicalFlowReplies.cpp
    connectors/StatnettFrequencyReplies.cpp
    connectors/StatnettForecastJobs.cpp
    connectors/StatnettForecastResults.cpp
)

qt_add_executable(greenpulse_monitor ${greenpulse_SOURCES})

target_include_directories(greenpulse_monitor PRIVATE 
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/monitor/h
    ${CMAKE_CURRENT_SOURCE_DIR}/telemetry
    ${CMAKE_CURRENT_SOURCE_DIR}/connectors
)

target_link_libraries(greenpulse_monitor PRIVATE
    Qt6::Core
    Qt6::Gui
    Qt6::Qml
    Qt6::Quick
    Qt6::Multimedia
    Qt6::Network
    PkgConfig::FFMPEG
)

if(APPLE)
    target_link_libraries(greenpulse_monitor PRIVATE "-framework IOKit" "-framework CoreFoundation")
endif()

qt_add_qml_module(greenpulse_monitor
    URI greenpulse
    VERSION 1.0
    QML_FILES qml/main.qml
)
