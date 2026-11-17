package com.kokekk.kokekbooru

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import java.nio.FloatBuffer

object OnnxBridge {
    private val env: OrtEnvironment by lazy { OrtEnvironment.getEnvironment() }
    private var session: OrtSession? = null
    private var inputName: String = "input_1:0"

    @JvmStatic
    @Synchronized
    fun loadModel(modelPath: String, useNnapi: Boolean): String {
        session?.close()
        session = null

        var activeProvider = "Android CPU"
        if (useNnapi) {
            try {
                val opts = OrtSession.SessionOptions()
                opts.addNnapi()
                opts.setIntraOpNumThreads(4)
                session = env.createSession(modelPath, opts)
                activeProvider = "Android NNAPI"
            } catch (_: Throwable) {
                session = null
            }
        }

        if (session == null) {
            val opts = OrtSession.SessionOptions()
            opts.setIntraOpNumThreads(4)
            session = env.createSession(modelPath, opts)
            activeProvider = "Android CPU"
        }

        inputName = session!!.inputNames.firstOrNull() ?: "input_1:0"
        return activeProvider
    }

    @JvmStatic
    @Synchronized
    fun runBatch(flatInput: FloatArray, batchSize: Int): FloatArray {
        val sess = session ?: throw IllegalStateException("ONNX session not loaded")
        val shape = longArrayOf(batchSize.toLong(), 448L, 448L, 3L)
        val fb = FloatBuffer.wrap(flatInput)
        OnnxTensor.createTensor(env, fb, shape).use { tensor ->
            sess.run(mapOf(inputName to tensor)).use { result ->
                val outVal = result[0].value
                @Suppress("UNCHECKED_CAST")
                return when (outVal) {
                    is Array<*> -> {
                        val rows = outVal as Array<FloatArray>
                        val cols = if (rows.isNotEmpty()) rows[0].size else 0
                        val flat = FloatArray(rows.size * cols)
                        for (r in rows.indices) {
                            System.arraycopy(rows[r], 0, flat, r * cols, cols)
                        }
                        flat
                    }
                    is FloatArray -> outVal
                    else -> FloatArray(0)
                }
            }
        }
    }
}
