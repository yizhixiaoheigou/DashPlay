package com.shilapi.xcertplay.transport

import android.hardware.usb.UsbConfiguration
import android.hardware.usb.UsbConstants
import android.hardware.usb.UsbEndpoint
import android.hardware.usb.UsbInterface

/**
 * Finds the NCM control/data interface pair inside an active iPhone configuration.
 *
 * LIVI claims the control interface, claims the data interface, and selects data alternate
 * setting 1, the setting that carries the bulk endpoints. This class only reads descriptors.
 */
object NcmFunctionDiscovery {
    const val CONTROL_CLASS = 0x02
    const val CONTROL_SUBCLASS = 0x0d
    const val DATA_CLASS = 0x0a
    const val APPLE_ETHERNET_CLASS = 0xff
    const val APPLE_ETHERNET_SUBCLASS = 0xfd
    const val APPLE_ETHERNET_PROTOCOL = 0x01
    const val DATA_ALTERNATE_SETTING = 1

    data class NcmFunction(
        val control: UsbInterface,
        val data: UsbInterface,
        val statusIn: UsbEndpoint?,
        val bulkIn: UsbEndpoint,
        val bulkOut: UsbEndpoint,
        val idleData: UsbInterface?,
    )

    fun find(configuration: UsbConfiguration): NcmFunction? {
        return findAll(configuration).firstOrNull()
    }

    fun findAll(configuration: UsbConfiguration): List<NcmFunction> {
        val all = interfaces(configuration)
        return all.filter {
            it.interfaceClass == CONTROL_CLASS && it.interfaceSubclass == CONTROL_SUBCLASS
        }.distinctBy { it.id }.mapNotNull { control ->
            // Each iPhone CDC function has an adjacent data interface. Never pair a second
            // control interface with the first function's endpoints.
            val data = all
                .filter { it.id == control.id + 1 && it.interfaceClass == DATA_CLASS && bulkEndpoints(it) != null }
                .minByOrNull { if (it.alternateSetting == DATA_ALTERNATE_SETTING) 0 else 1 }
                ?: return@mapNotNull null
            val endpoints = bulkEndpoints(data) ?: return@mapNotNull null
            val statusIn = (0 until control.endpointCount)
                .map(control::getEndpoint)
                .singleOrNull {
                    it.direction == UsbConstants.USB_DIR_IN &&
                        it.type == UsbConstants.USB_ENDPOINT_XFER_INT
                }
            val idle = all.firstOrNull { it.id == data.id && it.alternateSetting == 0 && it.endpointCount == 0 }
            NcmFunction(control, data, statusIn, endpoints.first, endpoints.second, idle)
        }
    }

    private fun interfaces(configuration: UsbConfiguration): List<UsbInterface> =
        (0 until configuration.interfaceCount).map(configuration::getInterface)

    private fun bulkEndpoints(usbInterface: UsbInterface): Pair<UsbEndpoint, UsbEndpoint>? {
        val endpoints = (0 until usbInterface.endpointCount).map(usbInterface::getEndpoint)
        val input = endpoints.singleOrNull {
            it.direction == UsbConstants.USB_DIR_IN && it.type == UsbConstants.USB_ENDPOINT_XFER_BULK
        }
        val output = endpoints.singleOrNull {
            it.direction == UsbConstants.USB_DIR_OUT && it.type == UsbConstants.USB_ENDPOINT_XFER_BULK
        }
        return if (input != null && output != null) input to output else null
    }
}
