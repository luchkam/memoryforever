# Overview

Memory Forever is a Telegram bot that creates AI-generated videos from user photos using the Runway API. The bot allows users to select different scenarios (hugs, cheek kisses, waving, stairs) and formats (full body, waist up, chest up) to generate short 5-10 second vertical videos. It's designed as a simple, interactive bot that transforms static photos into dynamic video memories.

# User Preferences

Preferred communication style: Simple, everyday language.

# System Architecture

## Bot Framework
- **Telegram Bot API**: Uses pyTelegramBotAPI (telebot) for handling user interactions
- **State Management**: Simple in-memory dictionary for tracking user sessions and workflow states
- **File Handling**: Local file system with organized directories for uploads and renders

## Workflow Design
- **Multi-step Process**: Guided user experience through scene selection → format selection → photo upload → video generation
- **Session State**: Tracks user progress through the video creation pipeline
- **Inline Keyboards**: Interactive buttons for scene and format selection

## Video Generation Pipeline
- **Image Preprocessing**: PIL-based image handling and format conversion
- **AI Video Generation**: Runway ML API integration using image-to-video generation
- **Output Specifications**: Vertical format (720x1280) optimized for mobile viewing
- **Duration Options**: Flexible 5-second and 10-second video lengths

## Content Management
- **Scene Templates**: Predefined scenarios with associated prompts and parameters
- **Format Options**: Multiple framing choices for different video compositions
- **Negative Prompting**: Quality control through negative prompt engineering

## Error Handling
- **Configuration Validation**: Environment variable checks with user-friendly error messages
- **API Integration**: Structured error handling for external service calls
- **File Management**: Organized directory structure for temporary file storage

# External Dependencies

## APIs and Services
- **Runway ML API**: Primary video generation service using gen4_turbo model
- **Telegram Bot API**: Message handling and user interaction platform
- **OpenAI API**: Configured but not currently implemented (future enhancement)

## Python Libraries
- **telebot**: Telegram Bot API wrapper for Python
- **PIL (Pillow)**: Image processing and manipulation
- **requests**: HTTP client for API communications
- **Standard libraries**: os, io, time, base64, uuid, datetime for core functionality

## Infrastructure Requirements
- **Environment Variables**: Secure API key management through environment configuration
- **File System**: Local storage for temporary image and video files
- **Network Access**: Outbound HTTPS for API communications with Runway and Telegram