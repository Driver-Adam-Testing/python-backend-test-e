import pytest  # noqa: I001

from anthropic.types.message import Message as AnthropicMessage
from openai.types.chat import (
    ChatCompletionMessage,
    ChatCompletionMessageToolCall,
    ParsedChatCompletionMessage,
)
from openai.types.chat.chat_completion_message_tool_call import (
    Function as OpenAIFunction,
)
from shared.v3.interfaces.llm_message import LlmMessage, MessageKind
from shared.v3.interfaces.llm_tool import LlmTool


class TestTool(LlmTool):
    """
    A test tool that returns a success message.
    The caller should set test_input to 'success' or 'failure'
    """

    test_input: str

    def _execute(self) -> LlmMessage:
        return self.to_tool_call_response_message()

    def to_tool_call_response_message(self) -> LlmMessage:
        return LlmMessage(
            content=f"{self.test_input}",
            message_kind=MessageKind.TOOL_CALL_RESPONSE,
        )


@pytest.fixture
def llm_message() -> LlmMessage:
    return LlmMessage(content="Hello, world!", message_kind=MessageKind.USER)


@pytest.fixture
def openai_chat_tool_call_message() -> ChatCompletionMessage:
    return ChatCompletionMessage(
        content=None,
        refusal=None,
        role="assistant",
        audio=None,
        function_call=None,
        tool_calls=[
            ChatCompletionMessageToolCall(
                id="1",
                function=OpenAIFunction(
                    name="TestTool",
                    arguments='{"test_input": "success"}',
                ),
                type="function",
            )
        ],
    )


@pytest.fixture
def openai_parsed_chat_completion_message() -> ParsedChatCompletionMessage:
    return ParsedChatCompletionMessage(
        content="Hello, world! - openai parsed chat completion",
        role="assistant",
    )


@pytest.fixture
def openai_chat_completion_message() -> ChatCompletionMessage:
    return ChatCompletionMessage(
        content="Hello, world! - openai chat completion",
        role="assistant",
    )


@pytest.fixture
def anthropic_message() -> AnthropicMessage:
    return AnthropicMessage(
        id="1",
        role="assistant",
        content=[{"type": "text", "text": "Hello, world! - anthropic"}],
        model="model_name",
        type="message",
        usage={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
    )


def test_llm_message_to_string(llm_message: LlmMessage) -> None:
    assert llm_message.content == "Hello, world!"
    assert llm_message.message_kind == MessageKind.USER


def test_from_openai_chat_completion_message(
    openai_chat_completion_message: ChatCompletionMessage,
) -> None:
    llm_message = LlmMessage.from_openai_chat_completion_message(
        openai_chat_completion_message
    )
    assert llm_message.content == "Hello, world! - openai chat completion"
    assert llm_message.message_kind == MessageKind.ASSISTANT


def test_from_anthropic_message(anthropic_message: AnthropicMessage) -> None:
    llm_message = LlmMessage.from_anthropic_message(anthropic_message)
    assert llm_message.content == "Hello, world! - anthropic"
    assert llm_message.message_kind == MessageKind.ASSISTANT


def test_tool_call_response_message(
    openai_chat_tool_call_message: ChatCompletionMessage,
) -> None:
    llm_message = LlmMessage.from_openai_chat_completion_message(
        openai_chat_tool_call_message,
        tool_types=[TestTool],
    )
    assert llm_message.message_kind == MessageKind.TOOL_CALL_REQUEST
    tool_requests = llm_message.tool_requests
    assert len(tool_requests) == 1
    assert tool_requests[0].name == TestTool.__name__
    assert tool_requests[0].arguments == '{"test_input": "success"}'
    tool_response: LlmMessage = tool_requests[0].parsed_tool._execute()
    assert tool_response.content == "success"
    assert tool_response.message_kind == MessageKind.TOOL_CALL_RESPONSE


def test_llm_message_from_openai_parsed_chat_completion_message(
    openai_parsed_chat_completion_message: ParsedChatCompletionMessage,
) -> None:
    llm_message = LlmMessage.from_openai_parsed_chat_completion_message(
        openai_parsed_chat_completion_message
    )
    assert llm_message.content == "Hello, world! - openai parsed chat completion"
    assert llm_message.message_kind == MessageKind.ASSISTANT


def test_llm_message_hash() -> None:
    llm_message_1 = LlmMessage(content="Hello, world!", message_kind=MessageKind.USER)
    llm_message_2 = LlmMessage(content="Hello, world!", message_kind=MessageKind.USER)
    assert hash(llm_message_1) == hash(llm_message_2)
