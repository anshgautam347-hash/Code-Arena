# SecureCode Arena

SecureCode Arena is a Django-based coding competition platform designed to conduct secure, timed programming assessments. It provides participant authentication, a coding editor, exam monitoring, code submissions, test-case evaluation integration, and leaderboard functionality.

## Features

- **User Authentication:** Secure login and participant access.
- **Timed Assessments:** Exam sessions with countdown timers.
- **Exam Monitoring:** Fullscreen-exit and tab-switch violation tracking.
- **Online Code Editor:** CodeMirror-based editor for coding questions.
- **Code Submissions:** Submit solutions and record submission details.
- **Test Case Evaluation:** Integration layer for the Judge0 code execution service.
- **Scoring and Results:** Support for evaluating submissions and displaying results.
- **Leaderboards:** View participant rankings.
- **Admin Management:** Review participants, submissions, and recorded violations.

## Technology Stack

- Python
- Django
- HTML and CSS
- JavaScript
- CodeMirror 5
- SQLite (development database)
- Judge0 (code execution integration)

## Getting Started

### 1. Clone the repository

```bash
git clone https://github.com/anshgautam347-hash/Code-Arena.git
cd Code-Arena
```

### 2. Install dependencies

Make sure Python is installed. Install the packages listed in the project's requirements file:

```bash
python -m pip install -r requirements.txt
```

### 3. Configure environment variables

Configure the environment variables required by your project. For code execution, set the `JUDGE0_URL` variable to your Judge0 service URL.

Keep API keys, credentials, and secret values private. Do not commit your `.env` file to GitHub.

### 4. Apply database migrations

```bash
python manage.py migrate
```

### 5. Create an administrator account

```bash
python manage.py createsuperuser
```

### 6. Start the development server

```bash
python manage.py runserver
```

Open the local development address shown in your terminal, usually:

http://127.0.0.1:8000/

## Code Execution Status

The platform includes a Judge0 integration layer. A working Judge0 service must be configured before submitted code can be executed and evaluated.

Without a configured runner, submissions may be recorded without actual code execution or scoring.

## Testing

Run Django's system checks:

```bash
python manage.py check
```

Run the project's automated tests:

```bash
python manage.py test
```

If the project provides a runner configuration check, run:

```bash
python manage.py check_runner
```

## Security Notes

- Never commit `.env` files, API keys, passwords, or Django production secrets.
- Use a strong, private `SECRET_KEY` in production.
- Set `DEBUG = False` for production deployment.
- Configure appropriate `ALLOWED_HOSTS` values.
- Use HTTPS and secure cookie settings in production.
- Treat fullscreen and tab-switch monitoring as deterrents, not as foolproof cheating prevention.
- Configure and test the code execution service before using the platform for real competitions.

## Project Status

SecureCode Arena is under active development. Features and deployment configuration may evolve as the project progresses.

## Author

**Ansh Gautam**

GitHub: https://github.com/anshgautam347-hash

