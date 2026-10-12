// Server -
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <string.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <arpa/inet.h>
#include <netinet/in.h>

#define PORT 8080
#define MAXLINE 1024

int main()
{
    int sockfd;
    char buffer[MAXLINE];
    struct sockaddr_in servaddr, cliaddr;
    socklen_t len;
    int n;

    // Create UDP socket
    sockfd = socket(AF_INET, SOCK_DGRAM, 0);

    if (sockfd < 0)
    {
        perror("Socket creation failed");
        exit(EXIT_FAILURE);
    }

    memset(&servaddr, 0, sizeof(servaddr));
    memset(&cliaddr, 0, sizeof(cliaddr));

    // Server information
    servaddr.sin_family = AF_INET;
    servaddr.sin_addr.s_addr = INADDR_ANY;
    servaddr.sin_port = htons(PORT);

    // Bind socket
    if (bind(sockfd, (struct sockaddr *)&servaddr,
             sizeof(servaddr)) < 0)
    {
        perror("Bind failed");
        close(sockfd);
        exit(EXIT_FAILURE);
    }

    printf("UDP Server started...\n");
    printf("Waiting for client...\n\n");

    len = sizeof(cliaddr);

    // 1. Receive Hello from client
    n = recvfrom(sockfd, buffer, MAXLINE - 1, 0,
                 (struct sockaddr *)&cliaddr, &len);

    buffer[n] = '\0';
    printf("Client: %s\n", buffer);

    // Server: Hello
    char *msg = "Hello!";

    sendto(sockfd, msg, strlen(msg), 0,
           (struct sockaddr *)&cliaddr, len);

    printf("Server: %s\n", msg);

    // 2. Receive How are you?
    n = recvfrom(sockfd, buffer, MAXLINE - 1, 0,
                 (struct sockaddr *)&cliaddr, &len);

    buffer[n] = '\0';
    printf("Client: %s\n", buffer);

    // Server: Fine
    msg = "Fine.";

    sendto(sockfd, msg, strlen(msg), 0,
           (struct sockaddr *)&cliaddr, len);

    printf("Server: %s\n", msg);

    // 3. Receive CN question
    n = recvfrom(sockfd, buffer, MAXLINE - 1, 0,
                 (struct sockaddr *)&cliaddr, &len);

    buffer[n] = '\0';
    printf("Client: %s\n", buffer);

    // Server: Yes
    msg = "Yes.";

    sendto(sockfd, msg, strlen(msg), 0,
           (struct sockaddr *)&cliaddr, len);

    printf("Server: %s\n", msg);

    // 4. Receive attending question
    n = recvfrom(sockfd, buffer, MAXLINE - 1, 0,
                 (struct sockaddr *)&cliaddr, &len);

    buffer[n] = '\0';
    printf("Client: %s\n", buffer);

    // Server: Yes
    msg = "Yes.";

    sendto(sockfd, msg, strlen(msg), 0,
           (struct sockaddr *)&cliaddr, len);

    printf("Server: %s\n", msg);

    // 5. Server asks final question
    msg = "Why should we chat?";

    sendto(sockfd, msg, strlen(msg), 0,
           (struct sockaddr *)&cliaddr, len);

    printf("Server: %s\n", msg);

    // 6. Receive client's answer
    n = recvfrom(sockfd, buffer, MAXLINE - 1, 0,
                 (struct sockaddr *)&cliaddr, &len);

    buffer[n] = '\0';
    printf("Client: %s\n", buffer);

    // 7. Server ends conversation
    msg = "Bye!";

    sendto(sockfd, msg, strlen(msg), 0,
           (struct sockaddr *)&cliaddr, len);

    printf("Server: %s\n", msg);

    printf("\nChat ended by server.\n");

    close(sockfd);

    return 0;
} 

// Client - 
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <string.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <arpa/inet.h>
#include <netinet/in.h>

#define PORT 8080
#define MAXLINE 1024

int main()
{
    int sockfd;
    char buffer[MAXLINE];
    struct sockaddr_in servaddr;
    socklen_t len;
    int n;

    // Create UDP socket
    sockfd = socket(AF_INET, SOCK_DGRAM, 0);

    if (sockfd < 0)
    {
        perror("Socket creation failed");
        exit(EXIT_FAILURE);
    }

    memset(&servaddr, 0, sizeof(servaddr));

    // Server information
    servaddr.sin_family = AF_INET;
    servaddr.sin_port = htons(PORT);

    inet_pton(AF_INET, "10.2.0.A3", &servaddr.sin_addr);

    len = sizeof(servaddr);

    // 1. Client: Hello
    char *msg = "Hello!";

    sendto(sockfd, msg, strlen(msg), 0,
           (struct sockaddr *)&servaddr, len);

    printf("Client: %s\n", msg);

    // Receive Hello
    n = recvfrom(sockfd, buffer, MAXLINE - 1, 0,
                 (struct sockaddr *)&servaddr, &len);

    buffer[n] = '\0';
    printf("Server: %s\n", buffer);

    // 2. Client: How are you?
    msg = "How are you?";

    sendto(sockfd, msg, strlen(msg), 0,
           (struct sockaddr *)&servaddr, len);

    printf("Client: %s\n", msg);

    // Receive Fine
    n = recvfrom(sockfd, buffer, MAXLINE - 1, 0,
                 (struct sockaddr *)&servaddr, &len);

    buffer[n] = '\0';
    printf("Server: %s\n", buffer);

    // 3. Client: Is today CN lab?
    msg = "Is today CN lab today?";

    sendto(sockfd, msg, strlen(msg), 0,
           (struct sockaddr *)&servaddr, len);

    printf("Client: %s\n", msg);

    // Receive Yes
    n = recvfrom(sockfd, buffer, MAXLINE - 1, 0,
                 (struct sockaddr *)&servaddr, &len);

    buffer[n] = '\0';
    printf("Server: %s\n", buffer);

    // 4. Client: Are you attending?
    msg = "Are you attending?";

    sendto(sockfd, msg, strlen(msg), 0,
           (struct sockaddr *)&servaddr, len);

    printf("Client: %s\n", msg);

    // Receive Yes
    n = recvfrom(sockfd, buffer, MAXLINE - 1, 0,
                 (struct sockaddr *)&servaddr, &len);

    buffer[n] = '\0';
    printf("Server: %s\n", buffer);

    // 5. Receive final question
    n = recvfrom(sockfd, buffer, MAXLINE - 1, 0,
                 (struct sockaddr *)&servaddr, &len);

    buffer[n] = '\0';
    printf("Server: %s\n", buffer);

    // Client answers
    msg = "Because communication is important!";

    sendto(sockfd, msg, strlen(msg), 0,
           (struct sockaddr *)&servaddr, len);

    printf("Client: %s\n", msg);

    // 6. Receive Bye
    n = recvfrom(sockfd, buffer, MAXLINE - 1, 0,
                 (struct sockaddr *)&servaddr, &len);

    buffer[n] = '\0';
    printf("Server: %s\n", buffer);

    // End only when server says Bye
    if (strcmp(buffer, "Bye!") == 0)
    {
        printf("\nChat ended by server.\n");
    }

    close(sockfd);

    return 0;
} 
