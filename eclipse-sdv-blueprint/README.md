
# A hybrid Cloud–Edge SDV Application Lifecycle Management - Phase 1

This blueprint demonstrates an end-to-end workflow for developing, validating and orchestrating Mixed-Critical Software-Defined Vehicle (SDV) applications across cloud and HPC edge device.

It showcases how SDV applications are built in the cloud, pushed to a registry, and deployed onto an in-vehicle HPC running AOS Core and digital.auto runtime components such as MQTT, KUKSA, and a Signal Gateway. Vehicle signals are exchanged across heterogeneous compute domains with HPCs and Zonal compute — through uProtocol and Zenoh.

# Sample Use Case


## Backend technology

1. App developer writes an application and validates in digital.auto playground
1. After successful validation, App developer publishes the application in a cloud App Registry
1. OEM deploys digital.auto sdv runtime into existing E/E architecture which abstracts the underlying vehicle complexity for applications.
1. OEM creates a AosCore instance with digital.auto runtime
1. OEM creates AosCloud configuration for app orchestration
1. OEM performs trial deployment from cloud App Registry and tests via digital.auto playground
1. OEM centrally deploys application from cloud App Registry into vehicle fleet (OTA)

# Architecture


# Getting started

## Prerequisites

## Demo

