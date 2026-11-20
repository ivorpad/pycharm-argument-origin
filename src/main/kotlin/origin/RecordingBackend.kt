package origin

import com.intellij.execution.ExecutionException
import com.intellij.execution.configurations.RunProfile
import com.intellij.execution.configurations.RunProfileState
import com.intellij.execution.executors.DefaultDebugExecutor
import com.intellij.execution.runners.ExecutionEnvironment
import com.intellij.execution.ui.RunContentDescriptor
import com.intellij.ide.plugins.PluginManagerCore
import com.intellij.openapi.extensions.PluginId
import com.intellij.util.execution.ParametersListUtil
import com.intellij.xdebugger.XSessionStartedResult
import com.jetbrains.python.sdk.PythonSdkUtil
import com.jetbrains.python.debugger.PyDebugBackendRunner
import com.jetbrains.python.debugger.PyDebugRunner
import com.jetbrains.python.debugger.PyDebuggerBackend
import com.jetbrains.python.run.PythonRunConfiguration
import org.jetbrains.concurrency.Promise
import org.jetbrains.concurrency.resolvedPromise

// These callbacks became part of PyDebugBackendRunner in 263. Declaring them
// here also lets us compile the same backend against the 262 SDK.
private interface SessionBackend {
    fun isApplicable(executorId: String, profile: RunProfile): Boolean
    fun startSession(environment: ExecutionEnvironment): Promise<RunContentDescriptor?>
}

class RecordingBackend : PyDebugRunner(), PyDebugBackendRunner, SessionBackend {
    override val backend = PyDebuggerBackend.PYDEVD

    override fun canRun(executorId: String, profile: RunProfile) = isApplicable(executorId, profile)

    override fun isApplicable(executorId: String, profile: RunProfile): Boolean =
        executorId == DefaultDebugExecutor.EXECUTOR_ID && profile is PythonRunConfiguration &&
            profile.envs["ARG_ORIGIN_RECORD"] != "0" && localScript(profile) && profile.scriptName != recordingLauncher()

    override fun startSession(environment: ExecutionEnvironment): Promise<RunContentDescriptor?> {
        val state = environment.state ?: return resolvedPromise(null)
        return execute(environment, state)
    }

    // Both runner APIs reach this hook. Keep the IDE's execution environment
    // and give pydevd a copy of the script configuration with recording enabled.
    override fun createSessionEx(state: RunProfileState, environment: ExecutionEnvironment): Promise<XSessionStartedResult> {
        val copy = recordingCopy(environment.runProfile as PythonRunConfiguration)
        val recordingState = copy.getState(environment.executor, environment)
            ?: throw ExecutionException("Could not prepare the Python recording session")
        return super.createSessionEx(recordingState, environment)
    }
}

private fun localScript(config: PythonRunConfiguration): Boolean {
    if (config.isModuleMode || config.scriptName.isNullOrBlank()) return false
    val sdk = config.sdk ?: return false
    return !PythonSdkUtil.isRemote(sdk)
}

private fun recordingLauncher() = PluginManagerCore.getPlugin(PluginId.getId("local.argument.origin"))!!
    .pluginPath.resolve("python/record.py").toString()

private fun recordingCopy(original: PythonRunConfiguration): PythonRunConfiguration {
    val config = original.clone() as PythonRunConfiguration
    config.scriptParameters = listOf(ParametersListUtil.join(original.scriptName), original.scriptParameters.orEmpty())
        .filter { it.isNotBlank() }.joinToString(" ")
    config.scriptName = recordingLauncher()
    return config
}
