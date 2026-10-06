#include <netinet/in.h>
#include <stdio.h>
#include <sys/socket.h>
#include <unistd.h>

static int close_fd(int fd, const char *label)
{
    if (fd < 0) {
	// 획득 못한 자원임
	return 0;
    }

    if (close(fd) == -1) {
	perror(label);
	return -1;
    }

    return 0;
}

int main(void)
{
    /**
     * FD: File Description - 프로세스가 열린 자원을 가리킬 때 사용하는 정수임
     * AF_INET: IPv4 체계 사용
     * SOCKET_STREAM: TCP socket
     */
    int server_fd = socket(AF_INET, SOCK_STREAM, 0);

    if (server_fd == -1) {
	perror("socket");
	return 1;
    }

    printf("Socket created: fd=%d\n", server_fd);

    /**
     * 소켓에 붙일 IPv4 주소
     */
    struct sockaddr_in address = {0};

    // IPv4로 맞춰줘야 함
    address.sin_family = AF_INET;
    // TODO: 실행 시점에 조절할 수 있도록 하기
    address.sin_port = htons(8080);
    // TODO: 실행 시점에 조절할 수 있도록 하기
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);

    /**
     * 준비한 주소를 여기서 커널에 연결함
     * (const struct sockaddr *): bind가 여러 주소 체계 받아서 타입 맞춰줘야 함
     * 성공하면 0, 아니면 -1
     */
    int result =
	bind(server_fd, (const struct sockaddr *)&address, sizeof(address));

    if (result == -1) {
	perror("bind");

	if (close(server_fd) == -1) {
	    perror("close after bind failure");
	}

	return 1;
    }

    printf("Socket bound to 127.0.0.1:8080\n");

    // @reference:
    // https://beej.us/guide/bgnet/html/split/system-calls-or-bust.html#listen
    // The number of connections allowed on the incoming queue라는데 몇이 적절한
    // 수치인지 모르겠음. 성능 따라 가변적으로 조절해야하나?
    const int backlog = 16;

    if (listen(server_fd, backlog) == -1) {
	perror("listen");

	// 아 이거 이제 좀 쓰기 귀찮음
	if (close(server_fd) == -1) {
	    perror("close after listen failure");
	}

	return 1;
    }

    printf("Waiting for a client...\n");

    int client_fd = accept(server_fd, NULL, NULL);

    if (client_fd == -1) {
	perror("accept");

	if (close(server_fd) == -1) {
	    perror("close after accept failure");
	}

	return 1;
    }

    int status = 0;

    if (close_fd(client_fd, "client") == -1) {
	status += 1;
    }
    if (close_fd(server_fd, "server") == -1) {
	status += 1;
    }

    return status;
}
